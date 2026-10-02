"""聊天 WebView API

为 Chat Window、Control Center、Bubble、Live2D 提供统一的聊天消息接口。
通过 ChatSession 管理消息状态，通过 AppState 创建任务。

职责：
  - send_message(): 发送用户消息并创建任务
  - get_messages(): 获取消息列表（含任务状态同步）
  - get_session_info(): 获取会话元信息
  - clear_session(): 清空会话
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import json
import logging
import os
import re
import shutil
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List
from uuid import uuid4

from apps.core.activity_store import get_activity_store
from apps.core.chat_session import (
    ChatMessage,
    ChatSession,
    MessageRole,
    MessageStatus,
    load_existing_chat_session,
)
from apps.core.executor import (
    execution_capabilities,
    user_task_unavailable_payload,
    user_task_unavailable_reason,
)
from apps.core.special_sessions import is_proactive_chat_session
from apps.locald.screenshot import ScreenCapturePermissionError, capture_screenshot_to_file
from apps.shell.agent.runtime.config import MAIN_CHAT_AGENT_ID
from apps.shell.agent.runtime.group_run_support import create_runnable_run
from apps.shell.agent.runtime.group_runs import project_group_terminal_after_member
from apps.shell.agent.runtime.run_group_attachments import (
    issue_run_group_child_attachment,
    normalize_run_group_child_identity,
)
from apps.shell.agent_runtime import AgentRuntimeError, get_agent_runtime_service
from apps.shell.native_capabilities import get_native_image_input_capability
from apps.shell.yachiyo_agent.daily_desktop import (
    DailyDesktopEntrypointRuntimePlan,
    daily_desktop_approval_or_submit_entrypoint_requests,
    daily_desktop_allowed_tools,
    daily_desktop_entrypoint_runtime_plan,
    daily_desktop_executable_entrypoint_requests,
    daily_desktop_requests_can_complete_without_model,
    daily_desktop_runtime_plan_prepared_for_execution,
    daily_desktop_safe_direct_entrypoint_requests,
    direct_browser_entrypoint_requests,
    entrypoint_plan_user_metadata,
    main_chat_entrypoint_allowed_tools,
    planner_first_daily_desktop_entrypoint_requests,
)
from apps.shell.yachiyo_agent.discovered_app_followups import (
    planner_discovered_app_followup_can_direct_execute,
)
from apps.shell.yachiyo_agent.desktop_plan_hints import hotkey_hint
from apps.shell.yachiyo_agent.desktop_permissions import (
    cached_desktop_permission_diagnostics as _cached_desktop_permission_diagnostics,
)
from apps.shell.yachiyo_agent.desktop_execution_policy import (
    desktop_provider_session_auto_start_default,
    desktop_provider_session_auto_start_recommended_for_requests,
    desktop_provider_session_strict_foreground_default,
    with_daily_entrypoint_desktop_execution_policy,
)
from apps.shell.yachiyo_agent.planner_execution import planner_orchestration_requests
from apps.shell.yachiyo_agent.planner_projection import planner_selection_payload
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner
from packages.protocol.enums import ErrorCode, TaskStatus, TaskType
from packages.security import contains_sensitive_text, redact_api_error_text


def desktop_permission_missing_by_capability(
    *,
    use_cache: bool = True,
) -> dict[str, list[str]]:
    """Legacy injection seam; always passive regardless of use_cache."""

    del use_cache
    return dict(
        _cached_desktop_permission_diagnostics().get("missing_permissions") or {}
    )

if TYPE_CHECKING:
    from apps.core.runtime import AppRuntime

logger = logging.getLogger(__name__)


class _ChatSessionNotFound(KeyError):
    """An explicit Chat projection target no longer exists."""


_MAX_CHAT_ATTACHMENTS = 4
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_MAX_ATTACHMENT_CACHE_BYTES = int(os.getenv("OHA_YACHIYO_ATTACHMENT_CACHE_BYTES", str(512 * 1024 * 1024)))
_MAX_ATTACHMENT_CACHE_AGE_SECONDS = int(
    os.getenv("OHA_YACHIYO_ATTACHMENT_CACHE_AGE_SECONDS", str(30 * 24 * 60 * 60))
)
_DATA_URL_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)
_VISION_ATTACHMENT_TOKEN_ESTIMATE = 85
_DAILY_DESKTOP_APP_FOLLOWUP_RECENT_LIMIT = 6
_DAILY_DESKTOP_APP_FOLLOWUP_MAX_CHARS = 120
_DAILY_DESKTOP_BROWSER_FOLLOWUP_RECENT_LIMIT = 6
_DAILY_DESKTOP_BROWSER_FOLLOWUP_MAX_CHARS = 120
_DAILY_DESKTOP_MUSIC_FOLLOWUP_RECENT_LIMIT = 6
_DAILY_DESKTOP_MUSIC_FOLLOWUP_MAX_CHARS = 80
_ENTRYPOINT_PLANNING_CONTEXT_MAX_CHARS = 600
_MAIN_CHAT_RUNNABLE_DESKTOP_EXECUTION_POLICY = {
    "mode": "preview_input",
    "prefer_isolated_desktop": True,
    "avoid_user_foreground_takeover": True,
    "require_sandbox_for_keyboard_mouse": True,
    "allow_media_control": True,
    "source": "daily_chat",
    "reason": (
        "Daily entrypoints should use structured desktop tools by default; "
        "keyboard and mouse capture still requires an isolated desktop provider."
    ),
}
_DAILY_DESKTOP_APP_CONTEXT_TOOLS = {
    "app.focus",
    "app.focus_and_safe_click",
    "app.focus_and_safe_key",
    "app.focus_and_hotkey",
    "app.focus_and_safe_scroll",
    "app.focus_and_safe_shortcut",
    "app.focus_and_safe_type_text",
    "app.focus_and_click_ui_element",
    "app.focus_and_type_into_ui_element",
    "app.open",
    "app.open_and_safe_click",
    "app.open_and_safe_key",
    "app.open_and_hotkey",
    "app.open_and_safe_scroll",
    "app.open_and_safe_shortcut",
    "app.open_and_safe_type_text",
    "app.open_and_click_ui_element",
    "app.open_and_type_into_ui_element",
    "app.show",
}
_DAILY_DESKTOP_BROWSER_APP_NAMES = {
    "Arc",
    "Brave Browser",
    "Firefox",
    "Google Chrome",
    "Microsoft Edge",
    "Safari",
}
_DAILY_DESKTOP_BROWSER_CONTEXT_TOOLS = {
    "browser.click",
    "browser.current_page",
    "browser.extract_text",
    "browser.open_url",
    "browser.open_url_and_extract_text",
    "browser.open_url_and_screenshot",
    "browser.screenshot",
    "browser.type_text",
}
_DAILY_DESKTOP_BROWSER_FOLLOWUP_TOOLS = {
    "browser.click",
    "browser.current_page",
    "browser.extract_text",
    "browser.screenshot",
    "browser.type_text",
}
_IMAGE_EXTENSIONS_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}
_TASK_TERMINAL_PROGRESS_METADATA_KEYS: tuple[str, ...] = (
    "pending_approval",
    "run_status",
    "run_progress_title",
    "run_progress_detail",
)
_GROUP_SUMMARY_METADATA_KEYS: tuple[str, ...] = (
    "group_agent_summary_for_task_id",
    "group_direct_agent_summary_for_message_id",
)
_GROUP_CONTEXT_MARKER = "[Oha-Yachiyo 群组上下文]"
_LEGACY_GROUP_CONTEXT_MARKER = "[Yachiyo 群组上下文]"
_GROUP_CONTEXT_MARKERS = (_GROUP_CONTEXT_MARKER, _LEGACY_GROUP_CONTEXT_MARKER)
_GROUP_FOLLOWUP_MARKER = "[Oha-Yachiyo 群组补充/纠偏]"
_LEGACY_GROUP_FOLLOWUP_MARKER = "[Yachiyo 群组补充/纠偏]"
_GROUP_FOLLOWUP_MARKERS = (_GROUP_FOLLOWUP_MARKER, _LEGACY_GROUP_FOLLOWUP_MARKER)
_GROUP_AGENT_UPSTREAM_MARKER = "[Oha-Yachiyo 群组执行约定]"
_DELEGATED_RUN_SUMMARY_LOCK = threading.RLock()
_GROUP_AGENT_SUMMARY_LOCK = threading.RLock()
_SEND_MESSAGE_SINGLEFLIGHT_LOCKS = tuple(
    threading.RLock()
    for _ in range(64)
)
_CHAT_SESSION_MUTATION_LOCKS = tuple(
    threading.RLock()
    for _ in range(64)
)
_CHAT_RUNTIME_SESSION_POINTER_LOCKS = tuple(
    threading.RLock()
    for _ in range(64)
)


def _has_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _text_before_first_marker(text: str, markers: tuple[str, ...]) -> str:
    marker_positions = [text.index(marker) for marker in markers if marker in text]
    if not marker_positions:
        return text
    return text[: min(marker_positions)]


def _terminal_task_message_metadata(metadata: dict[str, Any], run_status: str) -> dict[str, Any] | None:
    if not any(key in metadata for key in _TASK_TERMINAL_PROGRESS_METADATA_KEYS):
        return None
    next_metadata = dict(metadata)
    next_metadata["pending_approval"] = {}
    if "run_status" in next_metadata:
        next_metadata["run_status"] = run_status
    next_metadata.pop("run_progress_title", None)
    next_metadata.pop("run_progress_detail", None)
    return next_metadata


def _terminal_or_group_summary_metadata(metadata: dict[str, Any], run_status: str) -> dict[str, Any] | None:
    terminal_metadata = _terminal_task_message_metadata(metadata, run_status)
    if terminal_metadata is not None:
        return terminal_metadata
    if any(key in metadata for key in _GROUP_SUMMARY_METADATA_KEYS):
        return dict(metadata)
    return None


@dataclass(frozen=True)
class GroupDispatchDirective:
    """Structured internal contract for group chat dispatch."""

    kind: str
    target: str = ""
    runnable_id: str = ""
    goal: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind or "").strip())
        object.__setattr__(self, "target", str(self.target or "").strip())
        object.__setattr__(self, "runnable_id", str(self.runnable_id or "").strip())
        object.__setattr__(self, "goal", str(self.goal or "").strip())

    @property
    def target_label(self) -> str:
        return self.target or self.runnable_id or "Agent"

    def as_request(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "target": self.target,
            "runnable_id": self.runnable_id,
            "goal": self.goal,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_request().get(key, default)


def estimate_chat_message_tokens(message: Any) -> int:
    """Return a local token estimate for a stored or in-memory chat message."""
    content = str(getattr(message, "content", "") or "")
    attachments = _message_attachments(message)
    return _estimate_text_tokens(content) + _estimate_attachment_tokens(attachments)


def estimate_chat_tokens(messages: list[Any]) -> int:
    return sum(estimate_chat_message_tokens(message) for message in messages)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0

    tokens = 0
    ascii_run = 0

    def flush_ascii_run() -> None:
        nonlocal ascii_run, tokens
        if ascii_run:
            tokens += max(1, (ascii_run + 3) // 4)
            ascii_run = 0

    for char in text:
        if char.isspace():
            flush_ascii_run()
            continue
        codepoint = ord(char)
        if (
            0x3040 <= codepoint <= 0x30FF
            or 0x3400 <= codepoint <= 0x9FFF
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            flush_ascii_run()
            tokens += 1
        elif char.isalnum() or char in "_-":
            ascii_run += 1
        else:
            flush_ascii_run()
            tokens += 1
    flush_ascii_run()
    return tokens


def _estimate_attachment_tokens(attachments: list[dict]) -> int:
    total = 0
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        if str(attachment.get("kind") or "").lower() == "image":
            total += _VISION_ATTACHMENT_TOKEN_ESTIMATE
        spoken_text = str(attachment.get("spoken_text") or "")
        if spoken_text:
            total += _estimate_text_tokens(spoken_text)
    return total


def _message_attachments(message: Any) -> list[dict]:
    attachments = getattr(message, "attachments", None)
    if isinstance(attachments, list):
        return [item for item in attachments if isinstance(item, dict)]
    raw = getattr(message, "attachments_json", "[]")
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
_AUDIO_MIME_BY_EXTENSION = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}
_DESKTOP_SNAPSHOT_REQUEST_RE = re.compile(
    r"("
    r"(?:^|[\s，。！？、；;,.!?])"
    r"(?:请你?|麻烦你?|劳烦你?|帮我|帮忙|你(?:(?:可以|能不能|能否|能|帮我|来)|(?=(?:看|看看|查看|瞧|识别|分析|读|读取|扫一眼|检查|截)))|Yachiyo|八千代|Agent|agent|助手)"
    r".{0,18}(?:看|看看|查看|瞧|识别|分析|读|读取|扫一眼|检查|能看到|能看见|看得到)"
    r".{0,18}(?:桌面|屏幕|当前窗口|当前画面|截图|截屏)"
    r"|"
    r"(?:^|[\s，。！？、；;,.!?])"
    r"(?:请你?|麻烦你?|劳烦你?|帮我|帮忙|你(?:(?:可以|能不能|能否|能|帮我|来)|(?=(?:看|看看|查看|瞧|识别|分析|读|读取|扫一眼|检查|截)))|Yachiyo|八千代|Agent|agent|助手)"
    r".{0,18}(?:桌面|屏幕|当前窗口|当前画面|截图|截屏)"
    r".{0,18}(?:看|看看|查看|瞧|识别|分析|读|读取|检查|有什么|是什么|情况|状态|能看到|能看见|看得到)"
    r"|"
    r"(?:^|[\s，。！？、；;,.!?])"
    r"(?:请你?|麻烦你?|劳烦你?|帮我|帮忙|你(?:(?:可以|能不能|能否|能|帮我|来)|(?=(?:看|看看|查看|瞧|识别|分析|读|读取|扫一眼|检查|截)))|Yachiyo|八千代|Agent|agent|助手)"
    r".{0,18}(?:截(?:个|一张|一下)?图|截(?:个|一下)?屏|截图|截屏)"
    r"|"
    r"(?:please|can you|could you|would you|help me|agent|assistant|yachiyo)"
    r".{0,24}(?:look|see|view|read|analy[sz]e|inspect|screenshot|screen shot)"
    r".{0,24}(?:screen|desktop|window|screenshot)"
    r"|"
    r"(?:please|can you|could you|would you|help me|agent|assistant|yachiyo)"
    r".{0,24}(?:screen|desktop|window|screenshot)"
    r".{0,24}(?:look|see|view|read|analy[sz]e|inspect)"
    r"|"
    r"(?:screen|desktop|window|screenshot)"
    r".{0,12}(?:please|can you|could you|would you)"
    r".{0,24}(?:look|see|view|read|analy[sz]e|inspect)"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_CHAT_VISIBLE_ACTIVITY_PHASES = {"tool_start", "tool_complete"}
_ACTIVE_RUN_STATUSES = {"pending", "processing", "running", "approval_required"}


def _can_direct_execute_data_analysis_discovery(
    requests: list[dict[str, Any]],
    default_workdir: Path | None,
) -> bool:
    request = _data_analysis_discovery_request(requests)
    if request is None:
        return False
    if default_workdir is None:
        return False
    payload = request.get("input") if isinstance(request.get("input"), dict) else {}
    return _has_single_data_analysis_file(
        default_workdir,
        str(payload.get("path") or ""),
        pattern=str(payload.get("pattern") or ""),
        file_type=str(payload.get("file_type") or ""),
    )


def _data_analysis_discovery_request(
    requests: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if len(requests) not in {1, 2}:
        return None
    request = requests[0]
    if not (
        str(request.get("source") or "").strip() == "runtime_planner"
        and str(request.get("tool") or "").strip()
        in {"workspace.list", "fs.find_files", "file.search"}
    ):
        return None
    if len(requests) == 1:
        if not (
            str(request.get("planning_reason") or "").strip()
            == "planner_prefetch_data_source"
            and bool(request.get("continue_to_model"))
        ):
            return None
        return request

    analyze_request = requests[1]
    analyze_input = (
        analyze_request.get("input")
        if isinstance(analyze_request.get("input"), dict)
        else {}
    )
    if not (
        str(analyze_request.get("source") or "").strip() == "runtime_planner"
        and str(analyze_request.get("tool") or "").strip() == "data.analyze"
        and str(analyze_input.get("selection_source") or "").strip()
        == "workspace.list"
        and str(analyze_input.get("path") or "").strip()
        in {
            "<selected file from workspace.list>",
            "<selected files from workspace.list>",
        }
    ):
        return None
    return request


def _has_single_data_analysis_file(
    default_workdir: Path,
    path: str,
    *,
    pattern: str = "",
    file_type: str = "",
) -> bool:
    root = Path(default_workdir).expanduser().resolve()
    clean_path = str(path or "").strip()
    if Path(clean_path).is_absolute():
        return False
    target = (root / (clean_path or ".")).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False
    if not target.exists() or not target.is_dir():
        return False
    count = 0
    for child in target.iterdir():
        if (
            child.is_file()
            and _data_analysis_file_kind(child.name)
            and _data_analysis_file_matches_filter(
                child.name,
                pattern=pattern,
                file_type=file_type,
            )
        ):
            count += 1
            if count > 1:
                return False
    return count == 1


def _data_analysis_file_matches_filter(
    name: str,
    *,
    pattern: str = "",
    file_type: str = "",
) -> bool:
    clean_name = str(name or "").strip()
    clean_pattern = str(pattern or "").strip()
    if clean_pattern and not _data_analysis_file_matches_pattern(
        clean_name,
        clean_pattern,
    ):
        return False
    clean_type = str(file_type or "").strip().lower()
    if clean_type in {"csv", "tsv", "json", "jsonl", "xlsx"}:
        return _data_analysis_file_kind(clean_name) == clean_type
    if clean_type in {"spreadsheet", "table", "text_table", "data"}:
        return bool(_data_analysis_file_kind(clean_name))
    return True


def _data_analysis_file_matches_pattern(name: str, pattern: str) -> bool:
    brace = re.fullmatch(
        r"(?P<prefix>.*)\{(?P<items>[^{}]+)\}(?P<suffix>.*)",
        str(pattern or "").strip(),
    )
    if brace is None:
        return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())
    prefix = str(brace.group("prefix") or "")
    suffix = str(brace.group("suffix") or "")
    return any(
        fnmatch.fnmatchcase(
            name.casefold(),
            f"{prefix}{item.strip()}{suffix}".casefold(),
        )
        for item in str(brace.group("items") or "").split(",")
        if item.strip()
    )


def _data_analysis_file_kind(path: str) -> str:
    lowered = str(path or "").strip().lower()
    if lowered.endswith(".csv"):
        return "csv"
    if lowered.endswith(".tsv"):
        return "tsv"
    if lowered.endswith(".json"):
        return "json"
    if lowered.endswith(".jsonl"):
        return "jsonl"
    if lowered.endswith(".xlsx"):
        return "xlsx"
    return ""


_MAIN_MODEL_ALIASES = (
    "native chat",
    "oha chat",
    "main model",
    "main-model",
    "main",
    "主模型",
    "主助手",
    "八千代",
    "月見八千代",
    "月见八千代",
    "yachiyo",
    "oha",
)
_MAIN_MODEL_ALIAS_SEPARATORS = set(" \t\r\n:：,，、;；")


def _compact_preview(value: Any, limit: int = 72) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _compact_structured_preview(value: Any, limit: int = 240) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, (dict, list)):
        try:
            return _compact_preview(json.dumps(value, ensure_ascii=False, sort_keys=True), limit)
        except (TypeError, ValueError):
            pass
    return _compact_preview(value, limit)


def _looks_like_internal_protocol_preview(value: Any) -> bool:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("{", "[")):
        return True
    markers = (
        "<oha",
        "<native",
        "dispatch_group",
        "group_dispatch",
        "oha.group_dispatch",
        "native.group_dispatch",
        "run_oha",
        '"action"',
        "'action'",
        '"tool"',
        "'tool'",
        "tool_calls",
        '"function"',
        '"arguments"',
    )
    return any(marker in lowered for marker in markers)


def _search_snippet(value: str, query: str, *, limit: int = 96) -> str:
    text = " ".join(str(value or "").split())
    needle = " ".join(str(query or "").split()).strip()
    if not text:
        return ""
    if not needle:
        return _compact_preview(text, limit)
    index = text.lower().find(needle.lower())
    if index < 0:
        return _compact_preview(text, limit)
    side = max(12, (limit - len(needle)) // 2)
    start = max(0, index - side)
    end = min(len(text), index + len(needle) + side)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(text):
        snippet = f"{snippet}..."
    return snippet


def _is_chat_visible_activity(event: dict[str, Any]) -> bool:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    raw_visibility = event.get("visibility", metadata.get("visibility"))
    if raw_visibility is not None:
        visibility = str(raw_visibility or "").strip().lower()
        if visibility != "user":
            return False
    event_type = str(
        event.get("event_type")
        or metadata.get("event_type")
        or event.get("type")
        or metadata.get("type")
        or ""
    ).strip().lower()
    tool_name = str(event.get("tool_name") or "")
    normalized_event_type = re.sub(r"[\s_.-]+", "", event_type)
    normalized_tool = re.sub(r"[\s_.-]+", "", tool_name.strip().lower())
    if (
        normalized_event_type == "agentmodelfollowupcontext"
        or normalized_event_type.startswith("agentpostactionverification")
        or normalized_tool == "agentmodelfollowupcontext"
        or normalized_tool.startswith("agentpostactionverification")
    ):
        return False
    source = str(event.get("source") or metadata.get("source") or "").strip().lower()
    if source in {
        "runtime_post_action_auto_verify",
        "runtime_native_postcondition_receipt",
    }:
        return False
    phase = str(event.get("phase") or "")
    if phase not in _CHAT_VISIBLE_ACTIVITY_PHASES or not tool_name:
        return False
    if normalized_tool in {"sendmessage", "messagesend"}:
        return False
    # Rows created before the visibility column existed need a conservative
    # fallback. Observation/verifier probes remain available in Run Detail,
    # but are not conversational progress users should have to read.
    if normalized_tool in {
        "desktopinspectapp",
        "desktoplistapps",
        "desktopreadui",
        "desktoprunningapps",
        "desktopuielements",
        "desktopverify",
        "nativeverifier",
        "runtimeobservation",
        "runtimeverify",
    }:
        return False
    text = " ".join(str(event.get(key) or "") for key in ("title", "detail"))
    lowered = text.lower()
    internal_markers = (
        "<oha_group_dispatch",
        "<native_group_dispatch",
        "oha_group_dispatch",
        "native_group_dispatch",
        "oha.group_dispatch",
        "native.group_dispatch",
        "group.dispatch",
        "dispatch_group_agent",
        "run_oha_agent",
        "run_oha_workflow",
        "internal model instruction",
    )
    if any(marker in lowered for marker in internal_markers):
        return False
    return True


def _attachment_root() -> Path:
    root = Path(os.getenv("OHA_YACHIYO_HOME", os.path.expanduser("~/.oha-yachiyo"))) / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _attachment_public_url(attachment_id: str) -> str:
    bridge_url = os.getenv("OHA_YACHIYO_BRIDGE_URL", "http://127.0.0.1:8420").rstrip("/")
    return f"{bridge_url}/ui/chat/attachments/{attachment_id}"


def allocate_chat_attachment_path(session_id: str, suffix: str) -> tuple[str, Path]:
    """Allocate a stable attachment path under the chat attachment cache."""
    attachment_id = uuid4().hex
    normalized_suffix = suffix if str(suffix or "").startswith(".") else f".{suffix or 'bin'}"
    safe_suffix = re.sub(r"[^A-Za-z0-9.]+", "", normalized_suffix) or ".bin"
    session_dir = _attachment_root() / (session_id or "default")
    session_dir.mkdir(parents=True, exist_ok=True)
    return attachment_id, session_dir / f"{attachment_id}{safe_suffix}"


def chat_attachment_record(
    attachment_id: str,
    path: Path | str,
    *,
    kind: str,
    name: str,
    mime_type: str,
) -> dict[str, Any]:
    resolved = Path(path)
    return {
        "id": attachment_id,
        "kind": kind,
        "name": name or resolved.name,
        "mime_type": mime_type,
        "size": resolved.stat().st_size if resolved.exists() else 0,
        "path": str(resolved),
    }


def audio_mime_type_for_suffix(suffix: str) -> str:
    return _AUDIO_MIME_BY_EXTENSION.get(str(suffix or "").lower(), "audio/wav")


def _sanitize_attachment_name(value: str) -> str:
    name = Path(value or "image").name.strip() or "image"
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name)[:96] or "image"


def _cleanup_attachment_cache(protected_paths: set[Path] | None = None) -> None:
    """Keep image attachment storage bounded.

    Attachments live on disk for chat history previews.  This cleanup only runs
    after new attachments are saved, removes files older than the retention
    window first, then trims oldest files if the cache still exceeds the cap.
    """
    root = _attachment_root()
    protected = {path.resolve() for path in protected_paths or set()}
    now = time.time()
    files: list[tuple[float, int, Path]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            stat = path.stat()
        except OSError:
            continue
        if resolved in protected:
            continue
        files.append((stat.st_mtime, stat.st_size, path))

    for mtime, _size, path in files:
        if _MAX_ATTACHMENT_CACHE_AGE_SECONDS > 0 and now - mtime > _MAX_ATTACHMENT_CACHE_AGE_SECONDS:
            try:
                path.unlink()
            except OSError:
                pass

    if _MAX_ATTACHMENT_CACHE_BYTES <= 0:
        return

    remaining: list[tuple[float, int, Path]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            stat = path.stat()
        except OSError:
            continue
        if resolved in protected:
            continue
        remaining.append((stat.st_mtime, stat.st_size, path))

    total = sum(size for _mtime, size, _path in remaining)
    for _mtime, size, path in sorted(remaining, key=lambda item: item[0]):
        if total <= _MAX_ATTACHMENT_CACHE_BYTES:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


def _remove_attachment_session_dir(session_id: str) -> None:
    session_id = (session_id or "").strip()
    if not re.fullmatch(r"[a-f0-9]{8}", session_id):
        return
    target = _attachment_root() / session_id
    try:
        resolved = target.resolve()
        root = _attachment_root().resolve()
    except OSError:
        return
    if root not in resolved.parents or not resolved.exists():
        return
    shutil.rmtree(resolved, ignore_errors=True)


def _direct_input_entrypoint_requests(
    requests: list[dict[str, Any]],
    *,
    text: str = "",
) -> list[dict[str, Any]]:
    approval_or_submit_requests = daily_desktop_approval_or_submit_entrypoint_requests(
        requests,
        text=text,
    )
    if approval_or_submit_requests:
        return approval_or_submit_requests
    executable = daily_desktop_executable_entrypoint_requests(requests)
    if len(executable) == 2:
        first_tool = str(executable[0].get("tool") or "").strip()
        second_tool = str(executable[1].get("tool") or "").strip()
        if first_tool in {"desktop.list_apps", "desktop.running_apps"} and second_tool == "desktop.inspect_app":
            return _direct_app_ui_read_entrypoint_requests(executable[1])
    if len(executable) != 1:
        return []
    request = executable[0]
    tool_name = str(request.get("tool") or "").strip()
    if tool_name in {"desktop.safe_type_text"}:
        return executable
    return _direct_app_ui_read_entrypoint_requests(request)


def _direct_app_ui_read_entrypoint_requests(
    request: dict[str, Any],
) -> list[dict[str, Any]]:
    if str(request.get("tool") or "").strip() != "desktop.inspect_app":
        return []
    if bool(request.get("approval_required")) or bool(request.get("requires_approval")):
        return []
    risk_level = str(request.get("risk_level") or "").strip().lower()
    if risk_level in {"high", "critical"}:
        return []
    payload = request.get("input") if isinstance(request.get("input"), dict) else {}
    app_name = str(payload.get("app_name") or "").strip()
    if not app_name:
        return []
    role_filter = str(payload.get("role_filter") or "").strip()
    limit = payload.get("limit", 80)
    base = {
        "protocol": str(request.get("protocol") or "json_fallback").strip() or "json_fallback",
        "source": str(request.get("source") or "runtime_planner").strip() or "runtime_planner",
        "planning_reason": "explicit_full_plan",
    }
    ui_input: dict[str, Any] = {"limit": limit}
    if role_filter:
        ui_input["role_filter"] = role_filter
    return [
        {
            **base,
            "tool": "app.focus",
            "input": {"app_name": app_name},
        },
        {
            **base,
            "tool": "desktop.ui_elements",
            "input": ui_input,
            "foreground_app_context": "current_app",
        },
    ]


class ChatAPI:
    """聊天 API（供 WebView JavaScript 调用）"""

    def __init__(self, runtime: "AppRuntime") -> None:
        self._runtime = runtime
        self._session_override = threading.local()

    def _unavailable_response(self, reason: str) -> Dict[str, Any]:
        payload = user_task_unavailable_payload(self._runtime)
        if payload and getattr(getattr(self._runtime.task_runner, "executor", None), "code", ""):
            return {"ok": False, **payload}
        return {"ok": False, "error": reason}

    @staticmethod
    def _normalize_client_message_id(value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return ""
        normalized = normalized[:128]
        if contains_sensitive_text(normalized):
            raise AgentRuntimeError("client_message_id/idempotency_key 不能包含 API key、token 或其他敏感值")
        return normalized

    @staticmethod
    def _with_client_message_id(metadata: dict[str, Any] | None, client_message_id: str) -> dict[str, Any] | None:
        if not client_message_id:
            return metadata
        next_metadata = dict(metadata or {})
        next_metadata["client_message_id"] = client_message_id
        return next_metadata

    @staticmethod
    def _merge_user_metadata(
        metadata: dict[str, Any] | None,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(extra, dict) or not extra:
            return metadata
        merged = dict(extra)
        merged.update(dict(metadata or {}))
        return merged

    @staticmethod
    def _planner_first_daily_desktop_requests(
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        allowed_tools: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return planner_first_daily_desktop_entrypoint_requests(
            text,
            metadata=metadata,
            allowed_tools=allowed_tools or daily_desktop_allowed_tools(),
        )

    def _idempotent_message_response(self, client_message_id: str) -> Dict[str, Any] | None:
        if not client_message_id:
            return None
        for message in reversed(self._session.get_messages(0)):
            if message.role != MessageRole.USER:
                continue
            metadata = message.metadata if isinstance(message.metadata, dict) else {}
            if str(metadata.get("client_message_id") or "") != client_message_id:
                continue
            status = message.status.value if isinstance(message.status, MessageStatus) else str(message.status)
            desktop_snapshot_error = metadata.get("desktop_snapshot_error")
            response: Dict[str, Any] = {
                "ok": True,
                "client_message_id": client_message_id,
                "committed": True,
                "delivery_state": "accepted",
                "message_id": message.message_id,
                "task_id": message.task_id or "",
                "status": status,
                "attachments": self._serialize_attachments(message.attachments),
                "idempotent": True,
            }
            if isinstance(desktop_snapshot_error, dict):
                response["desktop_snapshot_error"] = desktop_snapshot_error
            retry_of_message_id = str(metadata.get("retry_of_message_id") or "").strip()
            if retry_of_message_id:
                response["source_message_id"] = retry_of_message_id
            if message.error:
                response["error"] = message.error
            return response
        return None

    def _client_message_requires_task_resume(self, client_message_id: str) -> bool:
        if not client_message_id:
            return False
        for message in reversed(self._session.get_messages(0)):
            if message.role != MessageRole.USER:
                continue
            metadata = message.metadata if isinstance(message.metadata, dict) else {}
            if str(metadata.get("client_message_id") or "") != client_message_id:
                continue
            return bool(
                not str(message.task_id or "").strip()
                and metadata.get("chat_delivery_requires_task") is True
            )
        return False

    @staticmethod
    def _accepted_send_response(
        response: Dict[str, Any],
        client_message_id: str,
        *,
        delivery_state: str = "accepted",
    ) -> Dict[str, Any]:
        return {
            **response,
            "ok": True,
            "client_message_id": client_message_id,
            "committed": True,
            "delivery_state": delivery_state,
        }

    @staticmethod
    def _not_committed_send_response(
        response: Dict[str, Any],
        client_message_id: str,
    ) -> Dict[str, Any]:
        return {
            **response,
            "ok": False,
            "client_message_id": client_message_id,
            "committed": False,
            "delivery_state": "not_committed",
        }

    def _repair_send_message_task_link(
        self,
        *,
        message_id: str,
        task_id: str,
        client_message_id: str,
        text: str,
        attachments: list[dict[str, Any]],
        metadata: dict[str, Any] | None,
    ) -> None:
        """Repair the narrow task-created/message-link-not-written fault window."""
        if not message_id or not task_id:
            return
        committed = self._idempotent_message_response(client_message_id)
        if committed is not None and str(committed.get("task_id") or "").strip():
            return
        try:
            if client_message_id:
                self._session.upsert_user_message_by_client_id(
                    text,
                    client_message_id=client_message_id,
                    task_id=task_id,
                    status=MessageStatus.PENDING,
                    attachments=attachments,
                    metadata=metadata,
                )
            else:
                self._session.link_message_to_task(message_id, task_id)
        except Exception:
            logger.warning(
                "消息已接收，但任务关联仍在等待同步: message_id=%s, task_id=%s",
                message_id,
                task_id,
            )

    def _store_user_message(
        self,
        content: str,
        attachments: list[dict[str, Any]],
        *,
        client_message_id: str,
        metadata: dict[str, Any] | None,
    ) -> str:
        if client_message_id:
            return self._session.upsert_user_message_by_client_id(
                content,
                client_message_id=client_message_id,
                status=MessageStatus.PENDING,
                attachments=attachments,
                metadata=metadata,
            )
        return self._session.add_user_message(
            content,
            attachments,
            metadata=metadata,
        )

    @property
    def _session(self) -> ChatSession:
        override = getattr(self._session_override, "value", None)
        if override is not None:
            return override
        return self._runtime.chat_session

    @property
    def _state(self):
        return self._runtime.state

    def _agent_runtime_service(self) -> Any:
        service = getattr(self._runtime, "agent_runtime_service", None)
        if service is not None:
            return service
        getter = getattr(self._runtime, "get_agent_runtime_service", None)
        if callable(getter):
            return getter()
        return get_agent_runtime_service()

    def _desktop_provider_session_owner_context(self) -> Any:
        try:
            service = self._agent_runtime_service()
        except Exception:
            return nullcontext()
        owner_token = str(
            getattr(service, "_desktop_provider_session_owner_token", "") or ""
        ).strip()
        if not owner_token:
            return nullcontext()
        from apps.shell.yachiyo_agent.isolated_provider_session import (
            desktop_provider_session_owner,
        )

        return desktop_provider_session_owner(owner_token)

    def _chat_store(self):
        store = getattr(self._runtime, "store", None)
        if store is not None:
            return store
        from apps.core.chat_store import get_chat_store

        return get_chat_store()

    def _activity_store(self):
        store = getattr(self._runtime, "activity_store", None)
        if store is not None:
            return store
        return get_activity_store()

    def _execute_direct_daily_desktop_task(
        self,
        *,
        task_id: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
        runtime_execution_envelope: dict[str, Any] | None = None,
        direct_tool_requests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        try:
            service = self._agent_runtime_service()
            if service is None:
                return None
            from apps.shell.yachiyo_agent.legacy_ports import LegacyChatTaskStarter
            from apps.shell.yachiyo_agent.task_cards import agent_task_snapshot_from_payload

            starter = LegacyChatTaskStarter(self._runtime, service)
            payload = starter.execute_existing_main_chat_task(
                task_id=task_id,
                conversation_id=str(getattr(self._session, "session_id", "") or ""),
                prompt=prompt,
                metadata=metadata,
                runtime_execution_envelope=runtime_execution_envelope,
                direct_tool_requests=direct_tool_requests,
            )
            if payload is None:
                return None
            return {
                "payload": payload,
                "agent_task": agent_task_snapshot_from_payload(payload).model_dump(mode="json"),
            }
        except Exception:
            logger.debug("主聊天日常桌面任务直接执行失败: %s", task_id, exc_info=True)
            return None

    def _planner_orchestration_entrypoint_requests(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return planner_orchestration_requests(
                text,
                metadata=self._planner_orchestration_runtime_metadata(metadata),
            )
        except Exception:
            logger.debug("Chat runtime planner orchestration candidates unavailable", exc_info=True)
            return []

    def _planner_orchestration_runtime_metadata(
        self,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        discovered = self._planner_orchestration_discovered_targets()
        if not discovered:
            return metadata
        next_metadata = dict(metadata or {})
        for key, value in discovered.items():
            if value and key not in next_metadata:
                next_metadata[key] = value
        return next_metadata

    def _planner_orchestration_discovered_targets(self) -> dict[str, list[dict[str, str]]]:
        try:
            service = self._agent_runtime_service()
        except Exception:
            logger.debug("Planner orchestration target discovery unavailable", exc_info=True)
            return {}
        return {
            "available_agent_groups": self._planner_orchestration_target_items(
                service,
                method_name="list_agent_groups",
                payload_key="groups",
                id_keys=("group_id", "agent_group_id", "id"),
                name_keys=("name", "title", "nickname"),
            ),
            "available_workflows": self._planner_orchestration_target_items(
                service,
                method_name="list_workflows",
                payload_key="workflows",
                id_keys=("workflow_id", "id"),
                name_keys=("name", "title", "nickname"),
            ),
        }

    def _planner_orchestration_target_items(
        self,
        service: Any,
        *,
        method_name: str,
        payload_key: str,
        id_keys: tuple[str, ...],
        name_keys: tuple[str, ...],
    ) -> list[dict[str, str]]:
        method = getattr(service, method_name, None)
        if not callable(method):
            return []
        try:
            payload = method()
        except Exception:
            logger.debug("Planner orchestration target list failed: %s", method_name, exc_info=True)
            return []
        items = payload.get(payload_key) if isinstance(payload, dict) else payload
        if not isinstance(items, (list, tuple)):
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            snapshot = self._snapshot_payload(item)
            if not snapshot or snapshot.get("enabled") is False:
                continue
            entry: dict[str, str] = {}
            for key in (*id_keys, *name_keys):
                value = " ".join(str(snapshot.get(key) or "").split()).strip()
                if not value or contains_sensitive_text(value):
                    continue
                entry[key] = value[:120]
            if not entry:
                continue
            dedupe_key = "|".join(entry.values()).casefold()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            result.append(entry)
        return result[:50]

    def _execute_planner_orchestration_runnable(
        self,
        text: str,
        requests: list[dict[str, Any]],
        *,
        client_message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        request = requests[0] if requests else {}
        if str(request.get("orchestration_kind") or "") != "workflow":
            return None
        payload = request.get("input") if isinstance(request.get("input"), dict) else {}
        target_name = str(payload.get("target_name") or "").strip()
        if not target_name:
            return None
        try:
            service = self._agent_runtime_service()
            runnable = service.resolve_runnable(name=target_name)
        except Exception:
            logger.debug("Planner workflow target could not be resolved: %s", target_name, exc_info=True)
            return None
        if not isinstance(runnable, dict) or runnable.get("kind") != "workflow":
            return None
        runnable_id = str(runnable.get("id") or runnable.get("workflow_id") or "").strip()
        if not runnable_id:
            return None
        orchestration_metadata = self._merge_user_metadata(
            metadata,
            self._planner_orchestration_user_metadata(requests),
        ) or {}
        return self._handle_runnable_command(
            text,
            [],
            runnable_id=runnable_id,
            client_message_id=client_message_id,
            metadata=orchestration_metadata,
        )

    @staticmethod
    def _planner_orchestration_user_metadata(
        requests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        request = requests[0] if requests else {}
        payload = request.get("input") if isinstance(request.get("input"), dict) else {}
        return {
            "yachiyo_runtime_planner": True,
            "yachiyo_intent_kind": str(request.get("intent_kind") or ""),
            "yachiyo_route_to_studio": bool(request.get("route_to_studio")),
            "yachiyo_decision_id": str(request.get("decision_id") or ""),
            "yachiyo_plan_id": str(request.get("plan_id") or ""),
            "yachiyo_orchestration_kind": str(request.get("orchestration_kind") or ""),
            "yachiyo_orchestration_target": str(payload.get("target_name") or "").strip(),
            "yachiyo_orchestration_planning_reason": str(request.get("planning_reason") or ""),
        }

    def _record_planner_group_run_start(
        self,
        *,
        task_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        if str(request.get("orchestration_kind") or "").strip() != "group_run":
            return None
        payload = request.get("input") if isinstance(request.get("input"), dict) else {}
        target_name = str(payload.get("target_name") or "").strip()
        if not target_name:
            return None

        group = self._planner_orchestration_group_target(target_name)
        if group is None:
            return None
        group_id = str(group.get("group_id") or group.get("agent_group_id") or group.get("id") or "").strip()
        if not group_id:
            return None

        objective = str(payload.get("objective") or "").strip()
        title = str(payload.get("title") or target_name).strip() or target_name
        try:
            from apps.shell.yachiyo_agent.legacy_ports import LegacyStudioPort
            from apps.shell.yachiyo_agent.studio_service import AgentStudioService

            studio = AgentStudioService(LegacyStudioPort(self._agent_runtime_service()))
            group_run = studio.start_group_run(
                {
                    "group_id": group_id,
                    "objective": objective,
                    "title": title,
                    "client_run_id": f"chat-{task_id}",
                    "metadata": {
                        "source": "chat_runtime_planner",
                        "task_id": task_id,
                        "decision_id": str(request.get("decision_id") or ""),
                        "plan_id": str(request.get("plan_id") or ""),
                    },
                }
            )
        except Exception as exc:
            logger.debug("Planner group run start failed: %s", target_name, exc_info=True)
            return self._record_planner_group_run_error(
                task_id=task_id,
                request=request,
                target_name=target_name,
                error=redact_api_error_text(exc),
            )

        group_run_payload = self._snapshot_payload(group_run)
        group_run_id = str(
            group_run_payload.get("group_run_id")
            or group_run_payload.get("run_group_id")
            or ""
        ).strip()
        run_group_id = str(group_run_payload.get("run_group_id") or group_run_id).strip()
        status = str(group_run_payload.get("status") or "running").strip() or "running"
        content = self._format_planner_group_run_started(
            request,
            group,
            group_run_payload,
            target_name=target_name,
        )
        self._state.update_task_status(
            task_id,
            self._task_status_from_group_run_status(status),
            result=content,
            progress_label="GroupRun",
        )
        assistant_id = self._session.upsert_assistant_message(
            task_id=task_id,
            content=content,
            status=self._message_status_from_group_run_status(status),
            metadata={
                "sender": self._main_model_sender_from_runtime(),
                "planner_orchestration": True,
                "planner_orchestration_started": True,
                "planner_orchestration_kind": "group_run",
                "planner_orchestration_target": target_name,
                "route_to_studio": bool(request.get("route_to_studio")),
                "intent_kind": str(request.get("intent_kind") or ""),
                "decision_id": str(request.get("decision_id") or ""),
                "plan_id": str(request.get("plan_id") or ""),
                "group_id": group_id,
                "group_run_id": group_run_id,
                "run_group_id": run_group_id,
                "group_run_status": status,
            },
        )
        return {
            "assistant_message_id": assistant_id,
            "status": self._response_status_from_group_run_status(status),
            "group_id": group_id,
            "group_run_id": group_run_id,
            "run_group_id": run_group_id,
            "group_run_status": status,
        }

    def _planner_orchestration_group_target(self, target_name: str) -> dict[str, Any] | None:
        target_key = self._planner_orchestration_lookup_key(target_name)
        if not target_key:
            return None
        try:
            from apps.shell.yachiyo_agent.legacy_ports import LegacyStudioPort
            from apps.shell.yachiyo_agent.studio_service import AgentStudioService

            studio = AgentStudioService(LegacyStudioPort(self._agent_runtime_service()))
            groups = studio.list_groups()
        except Exception:
            logger.debug("Planner group target list unavailable: %s", target_name, exc_info=True)
            return None

        for group_snapshot in groups:
            group = self._snapshot_payload(group_snapshot)
            if self._planner_orchestration_group_matches(group, target_key):
                return group
        return None

    @classmethod
    def _planner_orchestration_group_matches(
        cls,
        group: dict[str, Any],
        target_key: str,
    ) -> bool:
        values = (
            group.get("group_id"),
            group.get("agent_group_id"),
            group.get("id"),
            group.get("name"),
            group.get("title"),
            group.get("nickname"),
        )
        return any(cls._planner_orchestration_lookup_key(value) == target_key for value in values)

    @staticmethod
    def _planner_orchestration_lookup_key(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

    @staticmethod
    def _snapshot_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return dict(model_dump(mode="json"))
            except TypeError:
                return dict(model_dump())
        return {}

    def _record_planner_group_run_error(
        self,
        *,
        task_id: str,
        request: dict[str, Any],
        target_name: str,
        error: str,
    ) -> dict[str, Any]:
        content = f"已识别为 GroupRun / 多 Agent 编排请求，但启动「{target_name}」失败：{error}"
        self._state.update_task_status(
            task_id,
            TaskStatus.FAILED,
            result=content,
            error=error,
            progress_label="GroupRun",
        )
        assistant_id = self._session.upsert_assistant_message(
            task_id=task_id,
            content=content,
            status=MessageStatus.FAILED,
            metadata={
                "sender": self._main_model_sender_from_runtime(),
                "planner_orchestration": True,
                "planner_orchestration_kind": "group_run",
                "planner_orchestration_target": target_name,
                "planner_orchestration_error": True,
                "route_to_studio": bool(request.get("route_to_studio")),
                "intent_kind": str(request.get("intent_kind") or ""),
                "decision_id": str(request.get("decision_id") or ""),
                "plan_id": str(request.get("plan_id") or ""),
            },
        )
        return {
            "assistant_message_id": assistant_id,
            "status": "failed",
            "error": error,
        }

    @staticmethod
    def _task_status_from_group_run_status(status: str) -> TaskStatus:
        normalized = str(status or "").strip().lower()
        if normalized in {"completed", "succeeded", "success"}:
            return TaskStatus.COMPLETED
        if normalized in {"failed", "error"}:
            return TaskStatus.FAILED
        if normalized in {"cancelled", "canceled"}:
            return TaskStatus.CANCELLED
        return TaskStatus.RUNNING

    @staticmethod
    def _message_status_from_group_run_status(status: str) -> MessageStatus:
        normalized = str(status or "").strip().lower()
        if normalized in {"completed", "succeeded", "success"}:
            return MessageStatus.COMPLETED
        if normalized in {"failed", "error"}:
            return MessageStatus.FAILED
        return MessageStatus.PROCESSING

    @staticmethod
    def _response_status_from_group_run_status(status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized in {"completed", "succeeded", "success"}:
            return "completed"
        if normalized in {"failed", "error"}:
            return "failed"
        if normalized in {"cancelled", "canceled"}:
            return "cancelled"
        return "processing"

    @staticmethod
    def _format_planner_group_run_started(
        request: dict[str, Any],
        group: dict[str, Any],
        group_run: dict[str, Any],
        *,
        target_name: str,
    ) -> str:
        group_name = str(group.get("name") or target_name).strip() or target_name
        group_run_id = str(group_run.get("group_run_id") or group_run.get("run_group_id") or "").strip()
        status = str(group_run.get("status") or "running").strip() or "running"
        objective = str(
            (request.get("input") if isinstance(request.get("input"), dict) else {}).get("objective")
            or group_run.get("objective")
            or ""
        ).strip()
        suffix = f"目标：{objective}" if objective else "可在 Agent Studio 的 Groups / Run Timeline 中查看执行进度。"
        run_detail = f"（{group_run_id}）" if group_run_id else ""
        return f"已通过 Agent Studio 启动「{group_name}」GroupRun{run_detail}，当前状态：{status}。{suffix}"

    def _record_planner_orchestration_handoff(
        self,
        *,
        task_id: str,
        requests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        request = requests[0] if requests else {}
        group_run_start = self._record_planner_group_run_start(
            task_id=task_id,
            request=request,
        )
        if group_run_start is not None:
            return group_run_start
        content = self._format_planner_orchestration_handoff(request)
        self._state.update_task_status(
            task_id,
            TaskStatus.COMPLETED,
            result=content,
            progress_label="Agent Studio",
        )
        assistant_id = self._session.upsert_assistant_message(
            task_id=task_id,
            content=content,
            status=MessageStatus.COMPLETED,
            metadata={
                "sender": self._main_model_sender_from_runtime(),
                "planner_orchestration": True,
                "planner_orchestration_kind": str(request.get("orchestration_kind") or ""),
                "planner_orchestration_target": str(
                    (request.get("input") if isinstance(request.get("input"), dict) else {}).get("target_name")
                    or ""
                ),
                "route_to_studio": bool(request.get("route_to_studio")),
                "intent_kind": str(request.get("intent_kind") or ""),
                "decision_id": str(request.get("decision_id") or ""),
                "plan_id": str(request.get("plan_id") or ""),
            },
        )
        return {"assistant_message_id": assistant_id}

    @staticmethod
    def _format_planner_orchestration_handoff(request: dict[str, Any]) -> str:
        kind = str(request.get("orchestration_kind") or "").strip()
        payload = request.get("input") if isinstance(request.get("input"), dict) else {}
        target = str(payload.get("target_name") or "").strip()
        if kind == "workflow":
            if target:
                return (
                    f"已识别为 Workflow 编排请求，但当前没有找到名为「{target}」的 Workflow。"
                    "请在 Agent Studio 的 Workflow 面板选择或创建后运行。"
                )
            return "已识别为 Workflow 编排请求。请在 Agent Studio 的 Workflow 面板选择具体 Workflow 后运行。"
        if target:
            return (
                f"已识别为 GroupRun / 多 Agent 编排请求，但当前没有找到名为「{target}」的 Agent Group。"
                "请在 Agent Studio 的 Groups 面板选择群组后运行。"
            )
        return "已识别为 GroupRun / 多 Agent 编排请求。请在 Agent Studio 的 Groups 面板选择群组后运行。"

    def _warm_daily_desktop_permission_cache(
        self,
        requests: list[dict[str, Any]],
    ) -> None:
        if not requests:
            return
        try:
            desktop_permission_missing_by_capability(use_cache=True)
        except Exception:
            logger.debug("刷新桌面执行权限缓存失败", exc_info=True)

    def _daily_desktop_entrypoint_runtime_plan(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> DailyDesktopEntrypointRuntimePlan:
        allowed_daily_desktop_tools = daily_desktop_allowed_tools()
        try:
            runtime_service = self._agent_runtime_service()
        except Exception:
            runtime_service = None
        allowed_entrypoint_tools = main_chat_entrypoint_allowed_tools(
            runtime_service,
            fallback=allowed_daily_desktop_tools,
        )
        return daily_desktop_entrypoint_runtime_plan(
            text,
            metadata=metadata,
            allowed_tools=allowed_entrypoint_tools,
            metadata_allowed_tools=allowed_daily_desktop_tools,
            allow_legacy_fallback=False,
        )

    def _daily_desktop_entrypoint_requests(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Compatibility projection retained for legacy callers and tests."""

        allowed_daily_desktop_tools = daily_desktop_allowed_tools()
        try:
            runtime_service = self._agent_runtime_service()
        except Exception:
            runtime_service = None
        allowed_entrypoint_tools = main_chat_entrypoint_allowed_tools(
            runtime_service,
            fallback=allowed_daily_desktop_tools,
        )
        return planner_first_daily_desktop_entrypoint_requests(
            text,
            metadata=metadata,
            allowed_tools=allowed_entrypoint_tools,
            metadata_allowed_tools=allowed_daily_desktop_tools,
            execution_normalized=True,
            include_runtime_context=True,
            allow_legacy_fallback=False,
        )

    def _daily_desktop_followup_goal_text(
        self,
        text: str,
        current_context: dict[str, Any],
    ) -> str:
        """Resolve short daily desktop follow-ups into executable intents."""

        if current_context.get("conversation_kind") not in {"main", "agent"}:
            return text
        browser_followup = self._daily_desktop_browser_followup_goal_text(text)
        if browser_followup:
            return browser_followup
        app_followup = self._daily_desktop_app_followup_goal_text(text)
        if app_followup:
            return app_followup
        query = self._daily_desktop_music_followup_query(text)
        if not query:
            return text
        if self._planner_first_daily_desktop_requests(text):
            return text
        music_app = self._recent_daily_desktop_music_context_app_name()
        if not music_app:
            return text
        return f"用{music_app}播放{query}"

    def _daily_desktop_browser_followup_goal_text(self, text: str) -> str:
        candidate = self._daily_desktop_browser_followup_candidate(text)
        if not candidate:
            return ""
        if not self._recent_daily_desktop_browser_context_is_latest():
            return ""
        requests = self._planner_first_daily_desktop_requests(candidate)
        if not requests:
            return ""
        if not all(
            str(request.get("tool") or "").strip() in _DAILY_DESKTOP_BROWSER_FOLLOWUP_TOOLS
            for request in requests
        ):
            return ""
        return candidate

    @staticmethod
    def _daily_desktop_browser_followup_candidate(text: str) -> str:
        value = " ".join(str(text or "").split()).strip()
        if not value or len(value) > _DAILY_DESKTOP_BROWSER_FOLLOWUP_MAX_CHARS:
            return ""
        if "\n" in str(text or "") or re.search(r"https?://|www\.|/|\\", value, flags=re.IGNORECASE):
            return ""
        # This helper only expands genuinely context-free follow-ups.  An
        # explicit browser/page/app scope is already a complete planner input;
        # appending another "current page" clause can manufacture a bogus app
        # target such as "input on current" and leak content-only fields into
        # an app.focus request.
        if re.search(
            r"(?:网页|页面|浏览器|当前页|"
            r"\b(?:browser|web\s*page|webpage|current\s+page|"
            r"chrome|safari|firefox|edge|brave)\b)",
            value,
            flags=re.IGNORECASE,
        ):
            return ""
        lowered = value.lower()
        if lowered in {"算了", "算了吧", "不用了", "不要了", "取消", "不了", "不用", "no", "nope", "never mind"}:
            return ""
        if re.search(r"[?？]", value):
            return ""
        if re.search(
            r"(?:怎么|如何|为什么|为何|为啥|教程|说明|解释|how\s+to|why|explain|tutorial)",
            lowered,
        ):
            return ""
        read_patterns = (
            r"^(?:读取|读一下|读下|读一读|提取|抓取|获取)(?:一下|下)?(?:内容|正文|文字|文本)?$",
            r"^(?:read|extract|get)(?:\s+(?:content|text|page|this))?$",
        )
        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in read_patterns):
            return "读取当前网页内容"
        screenshot_patterns = (
            r"^(?:截图|截屏|屏幕截图|抓屏|截一下|截个图|截取)(?:一下|下)?$",
            r"^(?:screenshot|capture)(?:\s+(?:it|page|this))?$",
        )
        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in screenshot_patterns):
            return "当前网页截图"
        click_match = re.search(
            r"^(?:点击|点一下|点按|单击|双击|点)\s*(?P<label>[^。！？!?，,]+)$",
            value,
            flags=re.IGNORECASE,
        )
        if click_match:
            label = str(click_match.group("label") or "").strip()
            if label:
                prefix = "双击" if re.match(r"^(?:双击)", value) else "点击"
                return f"当前网页{prefix}{label}"
        english_click = re.search(r"^(?:click|press)\s+(?:the\s+)?(?P<label>[^.!?]+)$", value, flags=re.IGNORECASE)
        if english_click:
            label = str(english_click.group("label") or "").strip()
            if label:
                return f"click {label} on current page"
        if re.search(r"^(?:输入|填写|键入|打入|填入)\s*\S+", value, flags=re.IGNORECASE):
            return f"在当前网页{value}"
        english_type = re.search(r"^(?:type|enter|fill)\s+(?P<typed>[^.!?]+)$", value, flags=re.IGNORECASE)
        if english_type:
            typed = str(english_type.group("typed") or "").strip()
            if typed:
                return f"type {typed} into input on current page"
        return ""

    def _recent_daily_desktop_browser_context_is_latest(self) -> bool:
        for message in self._recent_daily_desktop_context_messages(
            _DAILY_DESKTOP_BROWSER_FOLLOWUP_RECENT_LIMIT
        ):
            raw_role = getattr(message, "role", "") or ""
            role = str(getattr(raw_role, "value", raw_role) or "").strip().lower()
            if role != MessageRole.USER.value:
                continue
            content = str(getattr(message, "content", "") or "")
            if self._message_has_daily_desktop_browser_context(content):
                return True
            if self._message_daily_desktop_app_context_name(content):
                return False
            if self._message_has_daily_desktop_music_intent(content):
                return False
        return False

    def _daily_desktop_app_followup_goal_text(self, text: str) -> str:
        if self._daily_desktop_explicit_modifier_hotkey(text):
            return ""
        clause = self._daily_desktop_app_followup_clause(text)
        if not clause:
            return ""
        app_name = self._recent_daily_desktop_app_context_name()
        if not app_name:
            return ""
        candidate = f"切到{app_name}，{clause}"
        requests = self._planner_first_daily_desktop_requests(candidate)
        if not requests:
            return ""
        if not self._daily_desktop_requests_target_app_context(requests, app_name):
            return ""
        return candidate

    @staticmethod
    def _daily_desktop_explicit_modifier_hotkey(text: str) -> bool:
        hotkey = hotkey_hint(text)
        if not hotkey:
            return False
        modifiers = hotkey.get("modifiers") if isinstance(hotkey, dict) else []
        return bool(modifiers)

    @staticmethod
    def _daily_desktop_app_followup_clause(text: str) -> str:
        value = " ".join(str(text or "").split()).strip()
        if not value or len(value) > _DAILY_DESKTOP_APP_FOLLOWUP_MAX_CHARS:
            return ""
        if "\n" in str(text or "") or re.search(r"https?://|www\.|/|\\", value, flags=re.IGNORECASE):
            return ""
        lowered = value.lower()
        if lowered in {"算了", "算了吧", "不用了", "不要了", "取消", "不了", "不用", "no", "nope", "never mind"}:
            return ""
        if re.search(r"[?？]", value):
            return ""
        if re.search(
            r"(?:怎么|如何|为什么|为何|为啥|教程|说明|解释|how\s+to|why|explain|tutorial)",
            lowered,
        ):
            return ""
        patterns = (
            r"^(?:搜索|搜一下|搜|查找|查一下|查查|检索)\s*\S+",
            r"^(?:输入|填写|键入|打入|填入)\s*\S+",
            r"^(?:点击|点一下|点按|单击|双击)\s*\S+",
            r"^(?:按下|按|发送|触发|快捷键|热键|组合键|按键)\s*\S+",
            r"^(?:复制|粘贴|全选|撤销|重做|查找|新建窗口|新建标签|刷新|返回|前进|发送|提交|确认|确定|回车)$",
            r"^(?:tab|escape|esc|enter|return|space|delete|backspace|up|down|left|right)$",
            r"^(?:上箭头|下箭头|左箭头|右箭头|空格|删除|退格)$",
            r"^(?:向上|向下|向左|向右)?(?:滚动|滑动|上滑|下滑|左滑|右滑)(?:一下|下|一页|半页)?$",
            r"^(?:scroll|page)\s+(?:up|down|left|right)$",
        )
        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns):
            return value
        return ""

    def _recent_daily_desktop_app_context_name(self) -> str:
        for message in self._recent_daily_desktop_context_messages(
            _DAILY_DESKTOP_APP_FOLLOWUP_RECENT_LIMIT
        ):
            raw_role = getattr(message, "role", "") or ""
            role = str(getattr(raw_role, "value", raw_role) or "").strip().lower()
            if role != MessageRole.USER.value:
                continue
            content = str(getattr(message, "content", "") or "")
            if self._message_has_daily_desktop_browser_context(content):
                return ""
            app_name = self._message_daily_desktop_app_context_name(
                content
            )
            if app_name:
                return app_name
        return ""

    @staticmethod
    def _message_daily_desktop_app_context_name(content: str) -> str:
        requests = ChatAPI._planner_first_daily_desktop_requests(
            ChatAPI._main_model_goal_text(content),
        )
        for request in reversed(requests):
            tool = str(request.get("tool") or "").strip()
            if tool not in _DAILY_DESKTOP_APP_CONTEXT_TOOLS:
                continue
            payload = request.get("input") if isinstance(request.get("input"), dict) else {}
            app_name = str(payload.get("app_name") or "").strip()
            if app_name and app_name != "Music":
                return app_name
        return ""

    @staticmethod
    def _message_has_daily_desktop_browser_context(content: str) -> bool:
        requests = ChatAPI._planner_first_daily_desktop_requests(
            ChatAPI._main_model_goal_text(content),
        )
        for request in requests:
            tool = str(request.get("tool") or "").strip()
            if tool in _DAILY_DESKTOP_BROWSER_CONTEXT_TOOLS:
                return True
            payload = request.get("input") if isinstance(request.get("input"), dict) else {}
            app_name = str(payload.get("app_name") or "").strip()
            if tool in _DAILY_DESKTOP_APP_CONTEXT_TOOLS and app_name in _DAILY_DESKTOP_BROWSER_APP_NAMES:
                return True
        return False

    @staticmethod
    def _daily_desktop_requests_target_app_context(
        requests: list[dict[str, Any]],
        app_name: str,
    ) -> bool:
        for request in requests:
            tool = str(request.get("tool") or "").strip()
            if not tool.startswith("app.focus"):
                continue
            payload = request.get("input") if isinstance(request.get("input"), dict) else {}
            if str(payload.get("app_name") or "").strip() == app_name:
                return True
        return False

    @staticmethod
    def _daily_desktop_music_followup_query(text: str) -> str:
        value = " ".join(str(text or "").split()).strip()
        if not value or len(value) > _DAILY_DESKTOP_MUSIC_FOLLOWUP_MAX_CHARS:
            return ""
        if "\n" in str(text or "") or re.search(r"https?://|www\.|/|\\", value, flags=re.IGNORECASE):
            return ""
        lowered = value.lower()
        if lowered in {"算了", "算了吧", "不用了", "不要了", "取消", "不了", "不用", "no", "nope", "never mind"}:
            return ""
        if re.search(r"[?？]", value):
            return ""
        if re.search(
            r"(?:怎么|如何|为什么|为何|为啥|教程|说明|解释|how\s+to|why|explain|tutorial)",
            lowered,
        ):
            return ""
        if re.search(
            r"(?:打开|启动|运行|拉起|开启|播放|放|搜索|查找|点击|输入|关闭|退出|隐藏|最小化|"
            r"open|launch|start|play|search|click|type|close|quit|hide|minimi[sz]e)",
            lowered,
        ):
            return ""
        query = re.sub(
            r"\s*(?:可以吗|好吗|好么|行吗|吗|嘛|呢|吧|please)[。！!]*$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        if len(query) < 2:
            return ""
        if re.fullmatch(r"[\W_]+", query, flags=re.IGNORECASE):
            return ""
        return query

    def _has_recent_daily_desktop_music_context(self) -> bool:
        return bool(self._recent_daily_desktop_music_context_app_name())

    def _recent_daily_desktop_music_context_app_name(self) -> str:
        for message in self._recent_daily_desktop_context_messages(
            _DAILY_DESKTOP_MUSIC_FOLLOWUP_RECENT_LIMIT
        ):
            raw_role = getattr(message, "role", "") or ""
            role = str(getattr(raw_role, "value", raw_role) or "").strip().lower()
            content = str(getattr(message, "content", "") or "").strip()
            if not content:
                continue
            if role == MessageRole.USER.value:
                app_name = self._message_daily_desktop_music_context_app_name(content)
                if app_name:
                    return app_name
            if role == MessageRole.ASSISTANT.value and self._assistant_mentions_music_followup(content):
                return "Apple Music"
        return ""

    def _recent_daily_desktop_context_messages(self, limit: int) -> list[Any]:
        try:
            messages = self._chat_store().load_messages(self._session.session_id, limit=limit)
        except Exception:
            messages = []
        if not messages:
            try:
                messages = list(self._session.get_messages())[-limit:]
            except Exception:
                messages = []
        return list(reversed(messages))

    @staticmethod
    def _message_has_daily_desktop_music_intent(content: str) -> bool:
        return bool(ChatAPI._message_daily_desktop_music_context_app_name(content))

    @staticmethod
    def _message_daily_desktop_music_context_app_name(content: str) -> str:
        requests = ChatAPI._planner_first_daily_desktop_requests(
            ChatAPI._main_model_goal_text(content),
        )
        for request in requests:
            tool = str(request.get("tool") or "").strip()
            payload = request.get("input") if isinstance(request.get("input"), dict) else {}
            if tool in {
                "media.apple_music_open_and_play",
                "media.apple_music_play",
                "media.apple_music_control",
                "media.system_control",
            }:
                return "Apple Music"
            if tool in {"media.music_app_open_and_play", "media.music_app_control"}:
                app_name = str(payload.get("app_name") or "").strip()
                if app_name and not app_name.startswith("<"):
                    return "Apple Music" if app_name == "Music" else app_name
                return "Apple Music"
            if tool in {"app.open", "app.focus", "app.show"}:
                app_name = str(payload.get("app_name") or "").strip()
                if app_name == "Music":
                    return "Apple Music"
        return ""

    @staticmethod
    def _assistant_mentions_music_followup(content: str) -> bool:
        text = " ".join(str(content or "").split()).strip()
        if not text:
            return False
        return bool(
            re.search(r"(?:apple\s*music|music|音乐|歌曲|歌)", text, flags=re.IGNORECASE)
            and re.search(r"(?:播放|想听|哪首|哪一首|歌名|曲名|song|track)", text, flags=re.IGNORECASE)
        )

    def _with_session(
        self,
        session_id: str,
        callback,
        *,
        require_existing: bool = False,
    ):
        """Run a small ChatAPI mutation against a specific persisted session."""
        session_id = str(session_id or "").strip()
        if not session_id:
            return callback()
        store = self._chat_store()
        current = self._runtime.chat_session
        if require_existing:
            session = load_existing_chat_session(
                store,
                session_id,
                current=current,
                fail_active_messages=False,
            )
            if session is None:
                raise _ChatSessionNotFound(session_id)
        elif current.session_id == session_id:
            session = current
        else:
            session = ChatSession(session_id=session_id)
            session.attach_store(
                store,
                load_existing=True,
                fail_active_messages=False,
            )

        sentinel = object()
        previous = getattr(self._session_override, "value", sentinel)
        self._session_override.value = session
        try:
            try:
                result = callback()
            except Exception:
                if require_existing and store.get_session(session_id) is None:
                    store.delete_session(session_id)
                    raise _ChatSessionNotFound(session_id) from None
                raise
            if require_existing and store.get_session(session_id) is None:
                store.delete_session(session_id)
                raise _ChatSessionNotFound(session_id)
            return result
        finally:
            if previous is sentinel:
                del self._session_override.value
            else:
                self._session_override.value = previous

    def _with_existing_session(self, session_id: str, callback) -> bool:
        """Run a late background projection only while its target exists."""
        try:
            self._with_session(session_id, callback, require_existing=True)
        except _ChatSessionNotFound:
            return False
        return True

    def _session_mutation_lock(self, session_id: str) -> threading.RLock:
        lock_key = (id(self._chat_store()), str(session_id or "").strip())
        return _CHAT_SESSION_MUTATION_LOCKS[
            hash(lock_key) % len(_CHAT_SESSION_MUTATION_LOCKS)
        ]

    def _runtime_session_pointer_lock(self) -> threading.RLock:
        return _CHAT_RUNTIME_SESSION_POINTER_LOCKS[
            hash(("runtime-session-pointer", id(self._runtime)))
            % len(_CHAT_RUNTIME_SESSION_POINTER_LOCKS)
        ]

    def send_message_in_session(
        self,
        session_id: str,
        text: str,
        attachments: list[dict] | None = None,
        *,
        runnable_id: str = "",
        client_message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Send while remaining bound to one existing persisted session."""

        target_session_id = str(session_id or "").strip()
        if not target_session_id:
            return self._not_committed_send_response(
                {"error": "会话不存在", "reason": "chat_session_deleted"},
                client_message_id,
            )

        # Lock order is always session mutation lease -> delivery singleflight.
        # delete_session takes only the outer lease, so it cannot invert this
        # order while an idempotent send is crossing its commit boundary.
        with self._session_mutation_lock(target_session_id):
            try:
                return self._with_session(
                    target_session_id,
                    lambda: self.send_message(
                        text,
                        attachments,
                        runnable_id=runnable_id,
                        client_message_id=client_message_id,
                        metadata=metadata,
                    ),
                    require_existing=True,
                )
            except _ChatSessionNotFound:
                return self._not_committed_send_response(
                    {
                        "error": "会话不存在",
                        "reason": "chat_session_deleted",
                        "session_id": target_session_id,
                    },
                    client_message_id,
                )

    def get_messages_in_session(
        self,
        session_id: str,
        limit: int = 0,
        anchor_message_id: str = "",
    ) -> Dict[str, Any]:
        """Read while remaining bound to one existing persisted session."""

        target_session_id = str(session_id or "").strip()
        if not target_session_id:
            return {
                "ok": False,
                "error": "会话不存在",
                "reason": "chat_session_deleted",
                "messages": [],
            }
        try:
            return self._with_session(
                target_session_id,
                lambda: self.get_messages(
                    limit,
                    anchor_message_id=anchor_message_id,
                ),
                require_existing=True,
            )
        except _ChatSessionNotFound:
            return {
                "ok": False,
                "error": "会话不存在",
                "reason": "chat_session_deleted",
                "session_id": target_session_id,
                "messages": [],
            }

    def send_message(
        self,
        text: str,
        attachments: list[dict] | None = None,
        *,
        runnable_id: str = "",
        client_message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Serialize one idempotent client delivery through its commit boundary."""
        try:
            idempotency_key = self._normalize_client_message_id(client_message_id)
        except AgentRuntimeError:
            # The implementation below owns the public validation response.
            return self._send_message_once(
                text,
                attachments,
                runnable_id=runnable_id,
                client_message_id=client_message_id,
                metadata=metadata,
            )
        if not idempotency_key:
            return self._send_message_once(
                text,
                attachments,
                runnable_id=runnable_id,
                client_message_id="",
                metadata=metadata,
            )
        lock_key = (
            id(self._chat_store()),
            self._session.session_id,
            idempotency_key,
        )
        lock = _SEND_MESSAGE_SINGLEFLIGHT_LOCKS[
            hash(lock_key) % len(_SEND_MESSAGE_SINGLEFLIGHT_LOCKS)
        ]
        with lock:
            return self._send_message_once(
                text,
                attachments,
                runnable_id=runnable_id,
                client_message_id=idempotency_key,
                metadata=metadata,
            )

    def _send_message_once(
        self,
        text: str,
        attachments: list[dict] | None = None,
        *,
        runnable_id: str = "",
        client_message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """发送用户消息并创建对应任务

        流程：
          1. 添加用户消息到 ChatSession
          2. 创建任务到 AppState（触发 TaskRunner 执行）
          3. 关联消息与任务
          4. 返回 message_id 和 task_id

        Args:
            text: 用户消息内容

        Returns:
            {"ok": True, "message_id": str, "task_id": str, "status": "pending"}
            或 {"ok": False, "error": str}
        """
        text = (text or "").strip()
        raw_attachments = attachments or []
        try:
            idempotency_key = self._normalize_client_message_id(client_message_id)
        except AgentRuntimeError as exc:
            return self._not_committed_send_response(
                {"error": redact_api_error_text(exc)},
                "",
            )
        if not text and not raw_attachments:
            return self._not_committed_send_response(
                {"error": "消息内容不能为空"},
                idempotency_key,
            )
        existing_response = self._idempotent_message_response(idempotency_key)
        if (
            existing_response is not None
            and not self._client_message_requires_task_resume(idempotency_key)
        ):
            return existing_response
        main_chat_runnable_entrypoint = (
            str(runnable_id or "").strip() == MAIN_CHAT_AGENT_ID
        )

        message_id = ""
        task_id = ""
        saved_attachments: list[dict[str, Any]] = []
        user_metadata: dict[str, Any] = {}
        try:
            current_context = self._session_context()
            group_presynced = False
            if current_context.get("conversation_kind") == "group":
                self._sync_current_session_status(notify_group_summary=False)
                current_context = self._session_context()
                group_presynced = True

            planner_orchestration_requests = []
            if (
                not raw_attachments
                and current_context.get("conversation_kind") in {"", "main", None}
            ):
                planner_orchestration_requests = self._planner_orchestration_entrypoint_requests(
                    self._entrypoint_planning_text(text, metadata),
                    metadata=metadata,
                )
                runnable_orchestration = self._execute_planner_orchestration_runnable(
                    text,
                    planner_orchestration_requests,
                    client_message_id=idempotency_key,
                    metadata=metadata,
                )
                if runnable_orchestration is not None:
                    return self._accepted_send_response(
                        runnable_orchestration,
                        idempotency_key,
                    )

            runnable_command = self._handle_runnable_command(
                text,
                raw_attachments,
                runnable_id=runnable_id,
                client_message_id=idempotency_key,
                metadata=metadata,
            )
            if runnable_command is not None:
                return self._accepted_send_response(
                    runnable_command,
                    idempotency_key,
                )

            current_context = self._session_context()
            if current_context.get("conversation_kind") == "group" and not group_presynced:
                self._sync_current_session_status(notify_group_summary=False)
                current_context = self._session_context()
            task_text = self._main_model_goal_text(
                self._entrypoint_planning_text(text, metadata)
            )
            task_text = self._daily_desktop_followup_goal_text(task_text, current_context)
            daily_planning_metadata = with_daily_entrypoint_desktop_execution_policy(
                metadata,
                surface="chat",
            )
            daily_desktop_runtime_plan = self._daily_desktop_entrypoint_runtime_plan(
                task_text,
                metadata=daily_planning_metadata,
            )
            if (
                not raw_attachments
                and current_context.get("conversation_kind") != "group"
                and self._daily_desktop_requests_can_direct_execute(
                    daily_desktop_runtime_plan.entrypoint_requests,
                    task_text,
                    metadata=daily_planning_metadata,
                )
            ):
                with self._desktop_provider_session_owner_context():
                    daily_desktop_runtime_plan = (
                        daily_desktop_runtime_plan_prepared_for_execution(
                            daily_desktop_runtime_plan,
                            metadata=daily_planning_metadata,
                        )
                    )
            daily_desktop_entrypoint_requests = list(
                daily_desktop_runtime_plan.entrypoint_requests
            )
            daily_desktop_requests = list(
                daily_desktop_runtime_plan.executable_requests
            )
            daily_desktop_runtime_envelope = dict(
                daily_desktop_runtime_plan.runtime_execution_envelope
            )
            blocked_daily_desktop_intent = bool(
                not raw_attachments
                and current_context.get("conversation_kind") != "group"
                and daily_desktop_runtime_envelope
                and daily_desktop_entrypoint_requests
                and not daily_desktop_requests
            )
            direct_daily_desktop_intent = (
                not raw_attachments
                and current_context.get("conversation_kind") != "group"
                and (
                    blocked_daily_desktop_intent
                    or self._daily_desktop_requests_can_direct_execute(
                        daily_desktop_requests,
                        task_text,
                        metadata=daily_planning_metadata,
                    )
                )
            )
            direct_daily_desktop_tool_requests = (
                direct_browser_entrypoint_requests(daily_desktop_requests, task_text)
                or daily_desktop_safe_direct_entrypoint_requests(daily_desktop_requests)
                or _direct_input_entrypoint_requests(
                    daily_desktop_requests,
                    text=task_text,
                )
                if direct_daily_desktop_intent
                else []
            )
            direct_daily_desktop_runtime_envelope = (
                daily_desktop_runtime_envelope
                if (
                    blocked_daily_desktop_intent
                    or direct_daily_desktop_tool_requests
                    or daily_desktop_requests_can_complete_without_model(
                        daily_desktop_requests,
                    )
                )
                else {}
            )
            direct_planner_orchestration_intent = (
                not raw_attachments
                and current_context.get("conversation_kind") != "group"
                and bool(planner_orchestration_requests)
            )
            if direct_daily_desktop_intent:
                self._warm_daily_desktop_permission_cache(daily_desktop_requests)
            unavailable_reason = user_task_unavailable_reason(self._runtime)
            if unavailable_reason and not direct_daily_desktop_intent and not direct_planner_orchestration_intent:
                return self._not_committed_send_response(
                    self._unavailable_response(unavailable_reason),
                    idempotency_key,
                )

            if raw_attachments and self._should_enforce_image_capability():
                image_input = get_native_image_input_capability()
                if image_input.get("can_attach_images") is False:
                    return self._not_committed_send_response(
                        {
                            "error": str(image_input.get("reason") or "当前 Native Agent 模型暂不支持图片输入"),
                            "image_input": image_input,
                        },
                        idempotency_key,
                    )
            saved_attachments = self._save_attachments(raw_attachments)
            if not text and saved_attachments:
                text = "请识别并分析这张图片。"
                task_text = text
            should_attach_desktop_snapshot = self._should_attach_desktop_snapshot(task_text, saved_attachments)
            if should_attach_desktop_snapshot and self._should_enforce_image_capability():
                image_input = get_native_image_input_capability()
                if image_input.get("can_attach_images") is False:
                    return self._not_committed_send_response(
                        {
                            "error": str(image_input.get("reason") or "当前 Native Agent 模型暂不支持图片输入"),
                            "image_input": image_input,
                        },
                        idempotency_key,
                    )
            task_description, saved_attachments, desktop_snapshot_error = self._attach_desktop_snapshot_if_needed(
                task_text,
                saved_attachments,
                should_attach=should_attach_desktop_snapshot,
            )
            user_metadata = self._group_followup_metadata_for_user_message(text, current_context)
            user_metadata = self._merge_user_metadata(user_metadata, metadata)
            if direct_daily_desktop_intent:
                user_metadata = self._merge_user_metadata(
                    user_metadata,
                    entrypoint_plan_user_metadata(daily_desktop_requests),
                )
            if direct_planner_orchestration_intent:
                user_metadata = self._merge_user_metadata(
                    user_metadata,
                    self._planner_orchestration_user_metadata(planner_orchestration_requests),
                )
            task_description = self._with_group_context_for_main_model(task_description, current_context)
            task_description = self._with_group_followup_context(task_description, user_metadata)
            direct_group_dispatch_directives: list[GroupDispatchDirective] = []
            if (
                current_context.get("conversation_kind") == "group"
                and not saved_attachments
                and not user_metadata
            ):
                direct_group_dispatch_directives = self._direct_group_dispatch_directives(
                    text,
                    current_context,
                )
            user_metadata = self._with_client_message_id(user_metadata, idempotency_key) or {}
            user_metadata = with_daily_entrypoint_desktop_execution_policy(
                user_metadata,
                surface="chat",
            )
            if main_chat_runnable_entrypoint and not direct_daily_desktop_intent:
                user_metadata = {
                    **user_metadata,
                    "desktop_provider_session_strict_foreground": True,
                    "desktop_provider_session_auto_start": True,
                    "desktop_execution_policy": dict(
                        _MAIN_CHAT_RUNNABLE_DESKTOP_EXECUTION_POLICY
                    ),
                }
            if desktop_provider_session_strict_foreground_default(user_metadata):
                user_metadata.setdefault("desktop_provider_session_strict_foreground", True)
            if desktop_provider_session_auto_start_recommended_for_requests(
                daily_desktop_requests,
            ):
                user_metadata.setdefault("desktop_provider_session_auto_start", True)
            elif desktop_provider_session_auto_start_default():
                user_metadata.setdefault("desktop_provider_session_auto_start", True)
            if desktop_snapshot_error:
                user_metadata["desktop_snapshot_error"] = desktop_snapshot_error
            if saved_attachments and not raw_attachments and self._should_enforce_image_capability():
                image_input = get_native_image_input_capability()
                if image_input.get("can_attach_images") is False:
                    return self._not_committed_send_response(
                        {
                            "error": str(image_input.get("reason") or "当前 Native Agent 模型暂不支持图片输入"),
                            "image_input": image_input,
                        },
                        idempotency_key,
                    )

            # 1. 添加用户消息
            user_metadata["chat_delivery_requires_task"] = True
            message_id = self._store_user_message(
                text,
                saved_attachments,
                client_message_id=idempotency_key,
                metadata=user_metadata or None,
            )

            # 2. 创建任务
            task = self._state.create_task(
                task_type=TaskType.GENERAL,
                description=task_description,
                attachments=saved_attachments,
                chat_session_id=self._session.session_id,
            )
            task_id = task.task_id
            if desktop_snapshot_error:
                self._record_desktop_snapshot_error_activity(task_id, desktop_snapshot_error)

            # 3. 关联消息与任务
            self._session.link_message_to_task(message_id, task_id)
            direct_daily_desktop_task: dict[str, Any] | None = None
            if direct_daily_desktop_intent:
                direct_daily_desktop_task = self._execute_direct_daily_desktop_task(
                    task_id=task_id,
                    prompt=task_text,
                    metadata=user_metadata,
                    runtime_execution_envelope=direct_daily_desktop_runtime_envelope,
                    direct_tool_requests=direct_daily_desktop_tool_requests or None,
                )
            direct_planner_orchestration_task: dict[str, Any] | None = None
            if direct_planner_orchestration_intent and direct_daily_desktop_task is None and not direct_group_dispatch_directives:
                direct_planner_orchestration_task = self._record_planner_orchestration_handoff(
                    task_id=task_id,
                    requests=planner_orchestration_requests,
                )
            if direct_group_dispatch_directives:
                assistant_id, native_status = self._start_native_direct_group_dispatch(
                    task=task, original_goal=task_text, source_message_id=message_id,
                    context=current_context,
                )
                return self._accepted_send_response({
                    "message_id": message_id, "task_id": task_id,
                    "assistant_message_id": assistant_id, "status": native_status,
                    "attachments": self._serialize_attachments(saved_attachments),
                    **({"desktop_snapshot_error": desktop_snapshot_error} if desktop_snapshot_error else {}),
                }, idempotency_key)
            if direct_planner_orchestration_task is not None:
                response = {
                    "ok": True,
                    "message_id": message_id,
                    "task_id": task_id,
                    "assistant_message_id": direct_planner_orchestration_task["assistant_message_id"],
                    "status": str(direct_planner_orchestration_task.get("status") or "completed"),
                    "planner_orchestration": True,
                    "attachments": self._serialize_attachments(saved_attachments),
                    **({"desktop_snapshot_error": desktop_snapshot_error} if desktop_snapshot_error else {}),
                }
                for key in ("group_id", "group_run_id", "run_group_id", "group_run_status", "error"):
                    value = direct_planner_orchestration_task.get(key)
                    if value:
                        response[key] = value
                return self._accepted_send_response(response, idempotency_key)
            if direct_daily_desktop_task is not None:
                payload = direct_daily_desktop_task["payload"]
                agent_task = direct_daily_desktop_task["agent_task"]
                status = str(agent_task.get("status") or payload.get("status") or "pending")
                assistant = self._session.get_assistant_message_for_task(task_id)
                return self._accepted_send_response({
                    "message_id": message_id,
                    "task_id": task_id,
                    "assistant_message_id": str(getattr(assistant, "message_id", "") or ""),
                    "status": status,
                    "run_id": str(payload.get("run_id") or ""),
                    "agent_task": agent_task,
                    "attachments": self._serialize_attachments(saved_attachments),
                    **({"desktop_snapshot_error": desktop_snapshot_error} if desktop_snapshot_error else {}),
                }, idempotency_key)
            if current_context.get("conversation_kind") == "group":
                self._create_pending_group_agent_summary_tasks()

            logger.info(
                "消息已发送: message_id=%s, task_id=%s, len=%d, attachments=%d",
                message_id,
                task_id,
                len(task_description),
                len(saved_attachments),
            )

            return self._accepted_send_response({
                "message_id": message_id,
                "task_id": task_id,
                "status": "pending",
                "attachments": self._serialize_attachments(saved_attachments),
                **({"desktop_snapshot_error": desktop_snapshot_error} if desktop_snapshot_error else {}),
            }, idempotency_key)

        except Exception as exc:
            safe_error = redact_api_error_text(exc)
            logger.error("发送消息失败: %s", safe_error)
            self._repair_send_message_task_link(
                message_id=message_id,
                task_id=task_id,
                client_message_id=idempotency_key,
                text=text,
                attachments=saved_attachments,
                metadata=user_metadata,
            )
            committed = self._idempotent_message_response(idempotency_key)
            if committed is not None:
                return self._accepted_send_response(
                    {
                        **committed,
                        "warning": "消息已接收，后续状态正在同步。",
                    },
                    idempotency_key,
                    delivery_state="accepted_uncertain",
                )
            return self._not_committed_send_response(
                {"error": "消息未发送，请重试。"},
                idempotency_key,
            )

    def send_runnable_message_in_session(
        self,
        session_id: str,
        text: str,
        *,
        runnable_id: str = "",
        client_message_id: str = "",
        metadata: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """Send an Agent/Workflow message through the existing Chat runtime path."""

        def _send() -> Dict[str, Any]:
            result = self.send_message(
                text,
                attachments or [],
                runnable_id=runnable_id,
                client_message_id=client_message_id,
                metadata=metadata,
            )
            if result.get("ok") is not False:
                return {**result, "session_id": self._session.session_id}
            return result

        return self._with_session(session_id, _send)

    def record_started_runnable_user_message_in_session(
        self,
        session_id: str,
        text: str,
        *,
        task_id: str,
        client_message_id: str,
        metadata: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """Record the canonical Chat user message after an external run starts."""
        target_session_id = str(session_id or "").strip()
        if not target_session_id:
            raise AgentRuntimeError("session_id 不能为空")
        if self._chat_store().get_session(target_session_id) is None:
            return {
                "ok": True,
                "suppressed": True,
                "reason": "chat_session_deleted",
                "task_id": str(task_id or ""),
                "session_id": target_session_id,
            }
        idempotency_key = self._normalize_client_message_id(client_message_id)
        if not idempotency_key:
            raise AgentRuntimeError("client_message_id 不能为空")

        def _record() -> Dict[str, Any]:
            canonical_metadata = self._with_client_message_id(
                metadata,
                idempotency_key,
            )
            message_id = self._session.upsert_user_message_by_client_id(
                text,
                client_message_id=idempotency_key,
                task_id=task_id,
                status=MessageStatus.PROCESSING,
                attachments=attachments,
                metadata=canonical_metadata,
            )
            return {
                "ok": True,
                "message_id": message_id,
                "task_id": task_id,
                "session_id": self._session.session_id,
                "status": MessageStatus.PROCESSING.value,
            }

        try:
            return self._with_session(
                target_session_id,
                _record,
                require_existing=True,
            )
        except KeyError:
            return {
                "ok": True,
                "suppressed": True,
                "reason": "chat_session_deleted",
                "task_id": str(task_id or ""),
                "session_id": target_session_id,
            }

    def summarize_delegated_run(
        self,
        run_id: str,
        *,
        conversation_id: str = "",
    ) -> Dict[str, Any]:
        """Create a main-model follow-up task for an auto-delegated Agent/Workflow run."""
        run_id = str(run_id or "").strip()
        if not run_id:
            return {"ok": False, "error": "Run ID 不能为空"}
        target_session_id = str(conversation_id or "").strip()
        if target_session_id and target_session_id != self._session.session_id:
            if self._chat_store().get_session(target_session_id) is None:
                return {"ok": False, "error": "会话不存在"}
            return self._with_session(
                target_session_id,
                lambda: self.summarize_delegated_run(run_id),
            )
        with _DELEGATED_RUN_SUMMARY_LOCK:
            existing_message = self._delegated_run_summary_message(run_id)
            if existing_message is not None:
                metadata = existing_message.metadata if isinstance(existing_message.metadata, dict) else {}
                return {
                    "ok": True,
                    "summary_created": False,
                    "message_id": existing_message.message_id,
                    "task_id": existing_message.task_id or "",
                    "run_id": run_id,
                    "run_group_id": str(metadata.get("run_group_id") or ""),
                    "run_status": str(metadata.get("run_status") or ""),
                    "source_task_id": str(metadata.get("delegated_run_source_task_id") or ""),
                    "reason": "already_exists",
                }
            try:
                run = self._agent_runtime_service().get_run(run_id)
            except KeyError:
                return {"ok": False, "error": "Run 不存在"}
            except AgentRuntimeError as exc:
                return {"ok": False, "error": redact_api_error_text(exc)}

            status = self._normalize_agent_run_status(str(run.get("status") or ""))
            run_group_id = str(run.get("run_group_id") or "")
            if status not in {"completed", "failed", "cancelled"}:
                return {
                    "ok": True,
                    "summary_created": False,
                    "run_id": run_id,
                    "run_group_id": run_group_id,
                    "run_status": status,
                    "reason": "not_terminal",
                }

            activity = self._delegated_run_activity(run_id)
            if activity is None:
                return {
                    "ok": True,
                    "summary_created": False,
                    "run_id": run_id,
                    "run_group_id": run_group_id,
                    "run_status": status,
                    "reason": "activity_not_found",
                }

            task = self._state.create_task(
                task_type=TaskType.GENERAL,
                description="Summarize the supplied context.",
                response_context=self._delegated_run_summary_task_description(run, activity),
                chat_session_id=self._session.session_id,
            )
            source_task_id = str(activity.get("task_id") or "")
            message_id = self._session.upsert_assistant_message(
                task_id=task.task_id,
                content="",
                status=MessageStatus.PROCESSING,
                metadata={
                    "sender": self._main_model_sender_from_runtime(),
                    "delegated_run_summary_for_run_id": run_id,
                    "delegated_run_source_task_id": source_task_id,
                    "run_id": run_id,
                    "run_group_id": run_group_id,
                    "run_status": status,
                },
            )
            return {
                "ok": True,
                "summary_created": True,
                "message_id": message_id,
                "task_id": task.task_id,
                "run_id": run_id,
                "run_group_id": run_group_id,
                "run_status": status,
                "source_task_id": source_task_id,
            }

    def _handle_runnable_command(
        self,
        text: str,
        raw_attachments: list[dict],
        *,
        runnable_id: str = "",
        client_message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        text = (text or "").strip()
        if str(runnable_id or "").strip() == MAIN_CHAT_AGENT_ID:
            return None
        if not runnable_id and self._parse_main_model_mention(text) is not None:
            return None

        current_context = self._session_context()
        explicit_target = bool(str(runnable_id or "").strip())
        if (
            not explicit_target
            and not self._has_chat_mention(text)
            and current_context.get("conversation_kind") != "agent"
        ):
            return None

        service = self._agent_runtime_service()
        name = ""
        user_goal = text
        runnable: dict[str, Any] | None = None

        try:
            if explicit_target:
                runnable = service.resolve_runnable(runnable_id=str(runnable_id or "").strip())
            elif self._has_chat_mention(text):
                parsed = service.parse_known_chat_runnable(text)
                if parsed is None:
                    return None
                name, user_goal = parsed
                explicit_target = True
                runnable = service.resolve_runnable(name=name)
            elif current_context.get("conversation_kind") == "agent":
                runnable = service.resolve_runnable(runnable_id=str(current_context.get("runnable_id") or ""))
                user_goal = text
        except AgentRuntimeError as exc:
            return self._record_runnable_error(
                text,
                redact_api_error_text(exc),
                context=current_context,
                client_message_id=client_message_id,
            )

        if runnable is None:
            return self._record_runnable_error(text, "未找到指定 Agent 或 Workflow", context=current_context, client_message_id=client_message_id)

        keep_workflow_group = (
            explicit_target
            and current_context.get("conversation_kind") == "workflow"
            and runnable.get("kind") == "agent"
            and bool(current_context.get("run_group_id"))
        )
        keep_manual_group = (
            explicit_target
            and current_context.get("conversation_kind") == "group"
            and runnable.get("kind") == "agent"
        )
        keep_group_workflow = (
            explicit_target
            and current_context.get("conversation_kind") == "group"
            and runnable.get("kind") == "workflow"
        )
        if keep_manual_group:
            if not self._group_context_contains_runnable(
                current_context,
                runnable,
                {
                    "target": name,
                    "runnable_id": str(runnable.get("id") or ""),
                },
            ):
                display_name = str(runnable.get("nickname") or runnable.get("name") or "Agent").strip() or "Agent"
                return self._record_runnable_error(
                    text,
                    f"{display_name} 不在当前群组中。请先在群组设置中加入后再 @。",
                    runnable=runnable,
                    context=current_context,
                    client_message_id=client_message_id,
                )
        if not keep_workflow_group and not keep_manual_group and not keep_group_workflow:
            self._prepare_runnable_session(
                runnable,
                explicit_target=explicit_target,
                current_context=current_context,
            )
            current_context = self._session_context()

        if raw_attachments:
            content = "Agent/Workflow 运行入口暂不支持附件。请把附件内容先整理成文字，或使用普通对话发送图片。"
            return self._record_runnable_error(text, content, runnable=runnable, context=current_context, client_message_id=client_message_id)
        if not user_goal:
            content = "运行目标不能为空。请在 Agent/Workflow 名称后写明需求。"
            return self._record_runnable_error(text, content, runnable=runnable, context=current_context, client_message_id=client_message_id)

        target = self._participant_for_runnable(runnable)
        # Conversation scope carries context, not authority to join an older
        # RunGroup. Every user-authored runnable request starts a fresh root
        # batch; only runtime-owned child orchestration may attach to one.
        run_group_id = ""
        user_metadata = {
            "target": target,
            "runnable_kind": runnable.get("kind") or "",
            "runnable_id": runnable.get("id") or "",
            "run_group_id": run_group_id,
        }
        user_metadata = self._merge_user_metadata(user_metadata, metadata) or {}
        runnable_daily_desktop_runtime_plan: (
            DailyDesktopEntrypointRuntimePlan | None
        ) = None
        runnable_daily_desktop_requests: list[dict[str, Any]] = []
        runnable_planning_goal = user_goal
        if (
            runnable.get("kind") == "agent"
            and (
                current_context.get("conversation_kind") != "group"
                or keep_manual_group
            )
        ):
            user_metadata = with_daily_entrypoint_desktop_execution_policy(
                user_metadata,
                surface=str(user_metadata.get("launcher_mode") or "agent"),
            )
            runnable_planning_goal = self._daily_desktop_followup_goal_text(
                user_goal,
                current_context,
            )
            runnable_daily_desktop_runtime_plan = (
                self._daily_desktop_entrypoint_runtime_plan(
                    runnable_planning_goal,
                    metadata=user_metadata,
                )
            )
            runnable_daily_desktop_runtime_plan = (
                daily_desktop_runtime_plan_prepared_for_execution(
                    runnable_daily_desktop_runtime_plan,
                    metadata=user_metadata,
                )
            )
            runnable_daily_desktop_requests = list(
                runnable_daily_desktop_runtime_plan.entrypoint_requests
            )
            if runnable_daily_desktop_requests:
                self._warm_daily_desktop_permission_cache(runnable_daily_desktop_requests)
                user_metadata = self._merge_user_metadata(
                    user_metadata,
                    entrypoint_plan_user_metadata(runnable_daily_desktop_requests),
                ) or {}
        user_metadata = self._with_client_message_id(user_metadata, client_message_id) or {}
        message_content = text or user_goal
        should_set_runnable_title = (
            current_context.get("conversation_kind") != "group"
            and self._session.message_count() == 0
            and bool(user_goal)
        )
        message_id = self._store_user_message(
            message_content,
            [],
            client_message_id=client_message_id,
            metadata=user_metadata,
        )
        if should_set_runnable_title:
            self._set_session_title_from_message(user_goal)
        upstream = self._chat_upstream_context()
        if current_context.get("conversation_kind") == "group" and runnable.get("kind") == "agent":
            upstream = self._with_group_context_for_agent_upstream(upstream, current_context, target)

        is_workflow = runnable.get("kind") == "workflow"

        if is_workflow:
            sender = self._participant_for_runnable(runnable)
            is_group_context = current_context.get("conversation_kind") == "group"
            assistant_id = self._session.add_assistant_message(
                "",
                metadata={
                    "sender": sender,
                    "runnable_kind": "workflow",
                    "runnable_id": runnable.get("id") or "",
                    "run_group_id": run_group_id,
                    "run_status": "processing",
                    "workflow_status": "processing",
                },
            )
            self._session.update_assistant_message(
                assistant_id,
                "",
                status=MessageStatus.PROCESSING,
            )
            callback_session_id = self._session.session_id

            def _on_workflow_run_complete(run_result: dict[str, Any]) -> None:
                def _sync_completed_workflow() -> None:
                    self._sync_runnable_run_status_to_messages()
                    if is_group_context:
                        self._create_pending_group_agent_summary_tasks()

                self._with_existing_session(callback_session_id, _sync_completed_workflow)
                logger.info("Workflow Run 异步完成: run_id=%s, status=%s", run_result.get("run_id"), run_result.get("status"))

            try:
                run = service.create_run_for_runnable_async(
                    runnable_id=str(runnable.get("id") or ""),
                    name=name,
                    user_goal=user_goal,
                    run_group_id=run_group_id,
                    upstream=upstream,
                    on_complete=_on_workflow_run_complete,
                )
            except AgentRuntimeError as exc:
                content = redact_api_error_text(exc)
                self._session.mark_message_completed(message_id)
                self._session.update_assistant_message(
                    assistant_id,
                    content,
                    status=MessageStatus.FAILED,
                    error=content,
                    metadata={"run_status": "failed", "workflow_status": "failed"},
                )
                return {
                    "ok": True,
                    "runnable_command": True,
                    "message_id": message_id,
                    "assistant_message_id": assistant_id,
                    "task_id": "",
                    "status": "completed",
                    "error": content,
                }

            self._session.mark_message_completed(message_id)
            runnable = run.get("runnable") or runnable
            if current_context.get("conversation_kind") == "group":
                self._bind_group_session_context(current_context, run_group_id=str(run.get("run_group_id") or ""))
                self._create_pending_group_agent_summary_tasks()
            else:
                self._bind_session_context("workflow", runnable, run_group_id=str(run.get("run_group_id") or ""))
            title, detail = self._workflow_run_progress_from_timeline(sender, run)
            self._session.update_assistant_message(
                assistant_id,
                "",
                status=MessageStatus.PROCESSING,
                metadata={
                    "sender": sender,
                    "runnable_kind": "workflow",
                    "runnable_id": runnable.get("id") or run.get("runnable_id") or "",
                    "run_id": run.get("run_id") or "",
                    "workflow_run_id": run.get("run_id") or "",
                    "run_group_id": run.get("run_group_id", ""),
                    "run_status": "processing",
                    "workflow_status": "processing",
                    "pending_approval": {},
                    "run_progress_title": title,
                    "run_progress_detail": detail,
                },
            )

            return {
                "ok": True,
                "runnable_command": True,
                "message_id": message_id,
                "assistant_message_id": assistant_id,
                "assistant_message_ids": [assistant_id],
                "task_id": "",
                "status": "processing",
                "run_id": run["run_id"],
                "run_group_id": run.get("run_group_id", ""),
                "run_status": "processing",
                "workflow_run_id": run["run_id"],
            }

        # Agent Run - 异步执行
        sender = self._participant_for_runnable(runnable)
        initial_content = ""
        is_group_context = current_context.get("conversation_kind") == "group"
        assistant_id = self._session.add_assistant_message(
            initial_content,
            metadata={
                "sender": sender,
                "runnable_kind": "agent",
                "runnable_id": runnable.get("id") or "",
                "run_group_id": run_group_id,
                "run_status": "processing",
                "conversation_kind": "group" if is_group_context else "",
                "group_goal": user_goal if is_group_context else "",
                "source_message_id": message_id if is_group_context else "",
            },
        )
        self._session.update_assistant_message(
            assistant_id,
            initial_content,
            status=MessageStatus.PROCESSING,
        )
        callback_session_id = self._session.session_id

        def _on_run_complete(run_result: dict[str, Any]) -> None:
            """Agent Run 完成后的回调"""
            self._with_existing_session(
                callback_session_id,
                lambda: self._update_agent_run_message_from_result(assistant_id, sender, run_result),
            )
            logger.info("Agent Run 异步完成: run_id=%s, status=%s", run_result.get("run_id"), run_result.get("status"))

        try:
            run_kwargs = {
                "runnable_id": str(runnable.get("id") or ""),
                "name": name,
                "user_goal": user_goal,
                "run_group_id": run_group_id,
                "upstream": upstream,
                "on_complete": _on_run_complete,
            }
            if (
                runnable_daily_desktop_runtime_plan is not None
                and runnable_daily_desktop_runtime_plan.has_plan
            ):
                run_kwargs["daily_desktop_policy_overlay"] = True
                run_kwargs["runtime_planner_entrypoint"] = True
                run_kwargs["metadata"] = dict(user_metadata)
                runtime_execution_envelope = (
                    runnable_daily_desktop_runtime_plan.runtime_execution_envelope
                )
                if runtime_execution_envelope:
                    run_kwargs["runtime_execution_envelope"] = dict(
                        runtime_execution_envelope
                    )
                elif runnable_daily_desktop_runtime_plan.executable_requests:
                    run_kwargs["direct_tool_requests"] = [
                        dict(request)
                        for request in (
                            runnable_daily_desktop_runtime_plan.executable_requests
                        )
                    ]
                if runnable_planning_goal != user_goal:
                    run_kwargs["daily_desktop_planning_context"] = runnable_planning_goal
            run = service.create_run_for_runnable_async(**run_kwargs)
        except AgentRuntimeError as exc:
            content = redact_api_error_text(exc)
            metadata_update: dict[str, Any] = {
                "run_status": "failed",
            }
            if is_group_context:
                agent_report = content
                content = self._group_agent_terminal_content(
                    sender,
                    "failed",
                    agent_report,
                    user_goal,
                )
                metadata_update.update({
                    "agent_report": agent_report,
                    "agent_report_status": "failed",
                })
            self._session.update_assistant_message(
                assistant_id,
                content,
                status=MessageStatus.FAILED,
                error=content,
                metadata=metadata_update,
            )
            if is_group_context:
                self._maybe_create_group_direct_agent_summary_task(assistant_id)
                self._create_pending_group_agent_summary_tasks()
            self._session.mark_message_completed(message_id)
            return {
                "ok": True,
                "runnable_command": True,
                "message_id": message_id,
                "assistant_message_id": assistant_id,
                "task_id": "",
                "status": "completed",
                "error": content,
            }

        self._session.mark_message_completed(message_id)
        runnable = run.get("runnable") or runnable
        self._attach_processing_agent_run_metadata(assistant_id, initial_content, run)

        if current_context.get("conversation_kind") == "group":
            self._bind_group_session_context(current_context, run_group_id=str(run.get("run_group_id") or ""))
            self._create_pending_group_agent_summary_tasks()
        elif current_context.get("conversation_kind") != "workflow":
            self._bind_session_context("agent", runnable, run_group_id=str(run.get("run_group_id") or ""))

        return {
            "ok": True,
            "runnable_command": True,
            "message_id": message_id,
            "assistant_message_id": assistant_id,
            "assistant_message_ids": [assistant_id],
            "task_id": "",
            "status": "processing",
            "run_id": run["run_id"],
            "run_group_id": run.get("run_group_id", ""),
            "run_status": "processing",
            "agent_run_id": run["run_id"],
        }

    def _prepare_runnable_session(
        self,
        runnable: dict[str, Any],
        *,
        explicit_target: bool,
        current_context: dict[str, Any],
    ) -> None:
        if not explicit_target:
            return
        if (
            current_context.get("conversation_kind") == "agent"
            and runnable.get("kind") == "agent"
            and current_context.get("runnable_id") == runnable.get("id")
        ):
            return

        if self._current_session_has_messages():
            start_new_session = getattr(self._runtime, "start_new_session", None)
            if callable(start_new_session):
                start_new_session()
            else:
                self._session.clear()

        if runnable.get("kind") == "agent":
            self._bind_session_context("agent", runnable, run_group_id="")
        elif runnable.get("kind") == "workflow":
            self._bind_session_context("workflow", runnable, run_group_id="")

    def _record_runnable_error(
        self,
        text: str,
        content: str,
        *,
        runnable: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        client_message_id: str = "",
    ) -> Dict[str, Any]:
        target = self._participant_for_runnable(runnable) if runnable else self._main_model_sender()
        context = context or self._session_context()
        message_content = (text or "").strip() or "（附件暂未发送给 Agent/Workflow）"
        message_id = self._store_user_message(
            message_content,
            [],
            client_message_id=client_message_id,
            metadata=self._with_client_message_id({
                "target": target,
                "runnable_kind": runnable.get("kind") if runnable else "",
                "run_group_id": context.get("run_group_id") or "",
            }, client_message_id),
        )
        self._session.mark_message_completed(message_id)
        assistant_id = self._session.add_assistant_message(
            content,
            error=content,
            metadata={
                "sender": self._main_model_sender(),
                "runnable_kind": runnable.get("kind") if runnable else "",
                "run_group_id": context.get("run_group_id") or "",
            },
        )
        if context.get("conversation_kind") == "group":
            self._create_pending_group_agent_summary_tasks()
        return {
            "ok": True,
            "runnable_command": True,
            "message_id": message_id,
            "assistant_message_id": assistant_id,
            "assistant_message_ids": [assistant_id],
            "task_id": "",
            "status": "completed",
            "error": content,
        }

    def _record_runnable_guidance(
        self,
        text: str,
        content: str,
        *,
        runnable: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        guidance_type: str = "",
        suggested_goal: str = "",
        client_message_id: str = "",
    ) -> Dict[str, Any]:
        target = self._participant_for_runnable(runnable) if runnable else self._main_model_sender()
        context = context or self._session_context()
        message_content = (text or "").strip() or "（空的 Agent/Workflow 指令）"
        message_id = self._store_user_message(
            message_content,
            [],
            client_message_id=client_message_id,
            metadata=self._with_client_message_id({
                "target": target,
                "runnable_kind": runnable.get("kind") if runnable else "",
                "run_group_id": context.get("run_group_id") or "",
            }, client_message_id),
        )
        self._session.mark_message_completed(message_id)
        assistant_id = self._session.add_assistant_message(
            content,
            metadata={
                "sender": self._main_model_sender(),
                "runnable_kind": runnable.get("kind") if runnable else "",
                "runnable_id": runnable.get("id") if runnable else "",
                "runnable_name": runnable.get("name") if runnable else "",
                "run_group_id": context.get("run_group_id") or "",
                "guidance_type": guidance_type,
                "suggested_goal": suggested_goal,
            },
        )
        if context.get("conversation_kind") == "group":
            self._create_pending_group_agent_summary_tasks()
        return {
            "ok": True,
            "runnable_command": True,
            "message_id": message_id,
            "assistant_message_id": assistant_id,
            "assistant_message_ids": [assistant_id],
            "task_id": "",
            "status": "completed",
        }

    def _append_agent_run_message(self, run: dict[str, Any], runnable: dict[str, Any]) -> str:
        sender = self._participant_for_runnable(runnable)
        status = str(run.get("status") or "")
        content = str(run.get("result") or "").strip()
        if status == "approval_required":
            content = self._approval_required_content(sender, run)
        if not content:
            content = self._run_status_sentence(sender.get("name") or "Agent", status)
        return self._session.add_assistant_message(
            content,
            error=content if status in {"failed", "cancelled"} else None,
            metadata={
                "sender": sender,
                "runnable_kind": "agent",
                "runnable_id": runnable.get("id") or run.get("runnable_id") or "",
                "run_id": run.get("run_id") or "",
                "run_group_id": run.get("run_group_id") or "",
                "run_status": status,
                "pending_approval": run.get("pending_approval") if isinstance(run.get("pending_approval"), dict) else {},
            },
        )

    def _append_workflow_run_messages(
        self,
        service: Any,
        run: dict[str, Any],
        runnable: dict[str, Any],
    ) -> list[str]:
        assistant_ids: list[str] = []
        for event in run.get("timeline") or []:
            if not isinstance(event, dict) or event.get("event") != "workflow.node.agent":
                continue
            child_run_id = str(event.get("child_run_id") or "").strip()
            if not child_run_id:
                continue
            try:
                child = service.get_run(child_run_id)
                child_runnable = service.resolve_runnable(runnable_id=str(child.get("runnable_id") or "")) or {}
            except Exception:
                logger.debug("读取 Workflow 子 Agent 运行失败: %s", child_run_id, exc_info=True)
                continue
            sender = self._participant_for_runnable(child_runnable)
            status = str(child.get("status") or "")
            content = str(child.get("result") or "").strip()
            if status == "approval_required":
                content = self._approval_required_content(sender, child)
            if not content:
                content = self._run_status_sentence(sender.get("name") or "Agent", status)
            child_artifact_count, child_artifact_summaries = self._visible_run_artifact_summaries(child)
            child_artifact_notice_count = child_artifact_count if status in {"completed", "failed", "cancelled"} else 0
            if child_artifact_notice_count > 0:
                content = self._append_artifact_notice(content, child_artifact_notice_count)
            assistant_ids.append(
                self._session.add_assistant_message(
                    content,
                    error=content if status in {"failed", "cancelled"} else None,
                    metadata={
                        "sender": sender,
                        "runnable_kind": "agent",
                        "runnable_id": child_runnable.get("id") or child.get("runnable_id") or "",
                        "run_id": child.get("run_id") or child_run_id,
                        "run_group_id": run.get("run_group_id") or child.get("run_group_id") or "",
                        "workflow_run_id": run.get("run_id") or "",
                        "workflow_node": event.get("detail") or "",
                        "run_status": status,
                        "pending_approval": child.get("pending_approval") if isinstance(child.get("pending_approval"), dict) else {},
                        "run_artifact_count": child_artifact_count,
                        "run_artifacts": child_artifact_summaries,
                    },
                )
            )

        workflow_status = str(run.get("status") or "")
        workflow_name = str(runnable.get("name") or run.get("runnable_name") or "Workflow")
        result_text = str(run.get("result") or "").strip()
        workflow_sender = self._participant_for_runnable(runnable)
        workflow_pending_approval = run.get("pending_approval") if isinstance(run.get("pending_approval"), dict) else {}
        waiting_child_approval = self._workflow_waiting_for_child_approval(run)
        workflow_artifact_count, workflow_artifact_summaries = self._visible_run_artifact_summaries(run)
        workflow_artifact_notice_count = (
            workflow_artifact_count if workflow_status in {"completed", "failed", "cancelled"} else 0
        )
        if workflow_status == "approval_required" and workflow_pending_approval.get("tool"):
            summary = self._approval_required_content(
                workflow_sender,
                run,
                goal=str(run.get("user_goal") or ""),
            )
        elif workflow_status == "approval_required" and waiting_child_approval:
            waiting_context = self._workflow_child_approval_context(run, service)
            summary = (
                f"{workflow_name} 正在等待子 Agent 审批。"
                f"{self._workflow_child_approval_notice(waiting_context)}\n\n"
                "处理上述子 Agent 的审批请求后，Workflow 会继续执行后续步骤。"
            )
        elif workflow_status == "completed" and assistant_ids:
            summary = self._workflow_terminal_content(
                workflow_sender,
                workflow_status,
                "",
                artifact_notice_count=workflow_artifact_notice_count,
            )
        elif result_text:
            if workflow_status in {"completed", "failed", "cancelled"}:
                summary = self._workflow_terminal_content(
                    workflow_sender,
                    workflow_status,
                    result_text,
                    artifact_notice_count=workflow_artifact_notice_count,
                    node_hint=self._workflow_terminal_node_hint(run, workflow_status),
                )
            else:
                summary_lines = [f"{workflow_name} {self._workflow_status_label(workflow_status)}。"]
                node_hint = self._workflow_terminal_node_hint(run, workflow_status)
                if node_hint:
                    summary_lines.append(node_hint)
                summary_lines.extend(["", result_text])
                summary = "\n".join(summary_lines)
        else:
            summary_lines = [f"{workflow_name} {self._workflow_status_label(workflow_status)}。"]
            node_hint = self._workflow_terminal_node_hint(run, workflow_status)
            if node_hint:
                summary_lines.append(node_hint)
            if workflow_artifact_notice_count > 0:
                summary_lines.append(f"产物：{workflow_artifact_notice_count} 个，见运行详情。")
            summary = "\n".join(summary_lines)
        message_run_status = "processing" if waiting_child_approval and not workflow_pending_approval.get("tool") else workflow_status
        workflow_metadata = {
            "sender": workflow_sender,
            "runnable_kind": "workflow",
            "runnable_id": runnable.get("id") or run.get("runnable_id") or "",
            "run_id": run.get("run_id") or "",
            "workflow_run_id": run.get("run_id") or "",
            "run_group_id": run.get("run_group_id") or "",
            "run_status": message_run_status,
            "workflow_status": workflow_status,
            "pending_approval": workflow_pending_approval,
            "run_artifact_count": workflow_artifact_count,
            "run_artifacts": workflow_artifact_summaries,
        }
        if waiting_child_approval:
            waiting_context = self._workflow_child_approval_context(run, service)
            if waiting_context.get("child_run_id"):
                workflow_metadata["workflow_waiting_child_run_id"] = waiting_context["child_run_id"]
            if waiting_context.get("tool"):
                workflow_metadata["workflow_waiting_tool"] = waiting_context["tool"]
            if waiting_context.get("workflow_node_label"):
                workflow_metadata["workflow_waiting_node"] = waiting_context["workflow_node_label"]
            if isinstance(waiting_context.get("pending_approval"), dict):
                workflow_metadata["workflow_waiting_pending_approval"] = waiting_context["pending_approval"]
        workflow_message_id = self._session.add_assistant_message(
            summary,
            error=summary if workflow_status in {"failed", "cancelled"} else None,
            metadata=workflow_metadata,
        )
        if workflow_status in _ACTIVE_RUN_STATUSES:
            self._session.update_assistant_message(
                workflow_message_id,
                summary,
                status=MessageStatus.PROCESSING,
                metadata=workflow_metadata,
            )
        assistant_ids.append(workflow_message_id)
        return assistant_ids

    def _sync_workflow_child_run_messages(
        self,
        service: Any,
        workflow_run: dict[str, Any],
    ) -> tuple[str, str] | None:
        workflow_run_id = str(workflow_run.get("run_id") or "").strip()
        if not workflow_run_id:
            return None

        group_run_ids: list[str] = []
        run_group_id = str(workflow_run.get("run_group_id") or "").strip()
        if run_group_id and hasattr(service, "get_run_group"):
            try:
                group = service.get_run_group(run_group_id)
                group_run_ids.extend(
                    str(run_id or "").strip()
                    for run_id in group.get("child_run_ids") or []
                    if str(run_id or "").strip() and str(run_id or "").strip() != workflow_run_id
                )
            except Exception:
                logger.debug("读取 Workflow Run Group 失败: %s", run_group_id, exc_info=True)

        timeline = [event for event in (workflow_run.get("timeline") or []) if isinstance(event, dict)]
        child_ids: list[str] = []
        node_event_by_child_id: dict[str, dict[str, Any]] = {}
        planned_agent_nodes: list[dict[str, Any]] = []
        for event in timeline:
            if str(event.get("event") or "") == "workflow.run.started":
                planned_agent_nodes = [
                    item
                    for item in event.get("workflow_path") or []
                    if isinstance(item, dict) and str(item.get("kind") or "") == "agent"
                ]
            if str(event.get("event") or "") != "workflow.node.agent":
                continue
            child_run_id = str(event.get("child_run_id") or "").strip()
            if not child_run_id:
                continue
            node_event_by_child_id[child_run_id] = event
            if child_run_id not in child_ids:
                child_ids.append(child_run_id)

        workflow_status = self._normalize_agent_run_status(str(workflow_run.get("status") or ""))
        if workflow_status in {"processing", "pending"}:
            # The parent timeline records an Agent node after it settles. While
            # it is running, discover only the next unknown Agent Run from the
            # workflow-owned group; later manual @Agent follow-ups must not be
            # mistaken for Workflow nodes.
            for candidate_run_id in group_run_ids:
                if candidate_run_id in child_ids:
                    continue
                try:
                    candidate = service.get_run(candidate_run_id)
                except Exception:
                    continue
                if str(candidate.get("kind") or "") == "agent_run":
                    child_ids.append(candidate_run_id)
                    break

        existing_by_run_id: dict[str, ChatMessage] = {}
        for message in self._session.get_all_messages():
            if message.role != MessageRole.ASSISTANT:
                continue
            metadata = message.metadata if isinstance(message.metadata, dict) else {}
            parent_run_id = str(metadata.get("workflow_parent_run_id") or metadata.get("workflow_run_id") or "").strip()
            if parent_run_id != workflow_run_id or metadata.get("runnable_kind") != "agent":
                continue
            child_run_id = str(metadata.get("run_id") or "").strip()
            if child_run_id:
                existing_by_run_id[child_run_id] = message

        active_progress: tuple[str, str] | None = None
        agent_index = 0
        for child_run_id in child_ids:
            try:
                child = service.get_run(child_run_id)
            except Exception:
                logger.debug("读取 Workflow 子 Agent Run 失败: %s", child_run_id, exc_info=True)
                continue
            if str(child.get("kind") or "") != "agent_run":
                continue
            node_info = dict(node_event_by_child_id.get(child_run_id) or {})
            if not node_info and agent_index < len(planned_agent_nodes):
                planned = planned_agent_nodes[agent_index]
                node_info = {
                    "workflow_node_id": planned.get("id") or "",
                    "workflow_node_kind": planned.get("kind") or "agent",
                    "workflow_node_label": planned.get("label") or "",
                    "workflow_node_task": planned.get("task") or "",
                }
            agent_index += 1
            try:
                child_runnable = service.resolve_runnable(runnable_id=str(child.get("runnable_id") or "")) or {}
            except Exception:
                logger.debug("读取 Workflow 子 Agent 失败: %s", child.get("runnable_id"), exc_info=True)
                child_runnable = {}
            sender = self._participant_for_runnable(child_runnable) or {
                "kind": "agent",
                "id": str(child.get("runnable_id") or ""),
                "name": str(child.get("runnable_name") or child.get("runnable_id") or "Agent"),
            }
            message = existing_by_run_id.get(child_run_id)
            projection_key = self._workflow_child_message_projection_key(
                workflow_run_id,
                child_run_id,
            )
            self._update_workflow_child_run_message(
                message.message_id if message is not None else None,
                sender,
                child,
                node_info,
                workflow_parent_run_id=workflow_run_id,
                projection_key=projection_key,
            )
            if self._normalize_agent_run_status(str(child.get("status") or "")) in _ACTIVE_RUN_STATUSES:
                active_progress = self._workflow_child_run_progress(sender, child, node_info)
        return active_progress

    @staticmethod
    def _workflow_child_message_projection_key(
        workflow_run_id: str,
        child_run_id: str,
    ) -> str:
        """Return the stable identity of one Workflow child chat projection."""
        return json.dumps(
            [
                "workflow_child",
                str(workflow_run_id or "").strip(),
                str(child_run_id or "").strip(),
                MessageRole.ASSISTANT.value,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _update_workflow_child_run_message(
        self,
        message_id: str | None,
        sender: dict[str, Any],
        child_run: dict[str, Any],
        node_info: dict[str, Any],
        *,
        workflow_parent_run_id: str = "",
        projection_key: str = "",
    ) -> str:
        status = self._normalize_agent_run_status(str(child_run.get("status") or "processing"))
        node_label = str(node_info.get("workflow_node_label") or node_info.get("detail") or "").strip()
        node_task = str(node_info.get("workflow_node_task") or "").strip()
        if not node_task:
            node_task = str(child_run.get("user_goal") or "").strip()
        pending_approval = child_run.get("pending_approval") if isinstance(child_run.get("pending_approval"), dict) else {}
        artifact_count, artifact_summaries = self._visible_run_artifact_summaries(child_run)
        metadata = {
            "sender": sender,
            "runnable_kind": "agent",
            "runnable_id": child_run.get("runnable_id") or "",
            "run_id": child_run.get("run_id") or "",
            "run_group_id": child_run.get("run_group_id") or "",
            "run_status": status,
            "pending_approval": pending_approval,
            "workflow_parent_run_id": str(node_info.get("workflow_parent_run_id") or "") or None,
            "workflow_node": node_label,
            "workflow_node_task": node_task,
            "run_artifact_count": artifact_count,
            "run_artifacts": artifact_summaries,
        }
        existing = self._message_metadata(message_id) if message_id else {}
        resolved_parent_run_id = str(
            existing.get("workflow_parent_run_id")
            or existing.get("workflow_run_id")
            or node_info.get("workflow_parent_run_id")
            or workflow_parent_run_id
            or ""
        ).strip()
        metadata["workflow_parent_run_id"] = resolved_parent_run_id
        metadata["workflow_run_id"] = resolved_parent_run_id
        if projection_key:
            metadata["assistant_projection_key"] = projection_key

        def write_projection(
            content: str,
            message_status: MessageStatus,
            *,
            error: str | None = None,
            metadata_update: dict[str, Any] | None = None,
        ) -> str:
            next_metadata = metadata_update or metadata
            if projection_key:
                return self._session.upsert_assistant_projection_message(
                    projection_key,
                    content,
                    status=message_status,
                    error=error,
                    metadata=next_metadata,
                    message_id=message_id or "",
                )
            if message_id:
                self._session.update_assistant_message(
                    message_id,
                    content,
                    status=message_status,
                    error=error,
                    metadata=next_metadata,
                )
                return message_id
            raise ValueError("projection_key is required for a new Workflow child message")

        if status == "approval_required":
            content = self._approval_required_content(sender, child_run, goal=node_task)
            return write_projection(
                content,
                MessageStatus.PROCESSING,
                error=None,
            )
        if status in {"processing", "pending"}:
            title, detail = self._workflow_child_run_progress(sender, child_run, node_info)
            return write_projection(
                "",
                MessageStatus.PROCESSING,
                error=None,
                metadata_update={
                    **metadata,
                    "run_progress_title": title,
                    "run_progress_detail": detail,
                },
            )

        result = str(child_run.get("result") or "").strip()
        content = self._workflow_child_terminal_content(
            sender,
            status,
            result,
            node_label=node_label,
            node_task=node_task,
            artifact_notice_count=artifact_count,
        )
        return write_projection(
            content,
            self._message_status_for_run_status(status),
            error=content if status in {"failed", "cancelled"} else None,
        )

    @staticmethod
    def _workflow_child_run_progress(
        sender: dict[str, Any],
        child_run: dict[str, Any],
        node_info: dict[str, Any],
    ) -> tuple[str, str]:
        name = str(sender.get("nickname") or sender.get("name") or child_run.get("runnable_name") or "Agent").strip() or "Agent"
        node_label = str(node_info.get("workflow_node_label") or node_info.get("detail") or "").strip()
        node_task = str(node_info.get("workflow_node_task") or child_run.get("user_goal") or "").strip()
        task_preview = _compact_preview(node_task, 160)
        node_text = f"节点「{node_label}」" if node_label else "当前 Workflow 节点"
        detail = f"{name} 正在执行{node_text}"
        if task_preview:
            detail = f"{detail}：{task_preview}"
        return "Workflow 正在执行 Agent", detail

    @staticmethod
    def _run_status_sentence(name: str, status: str) -> str:
        normalized = "processing" if status == "running" else status
        if normalized == "completed":
            return f"{name} 已完成，但没有返回内容。"
        if normalized == "approval_required":
            return f"{name} 等待工具审批。"
        if normalized in {"processing", "pending"}:
            return ""
        if normalized == "cancelled":
            return f"{name} 已取消。"
        if normalized == "failed":
            return f"{name} 执行失败。"
        return f"{name} 状态：{normalized or 'unknown'}。"

    @staticmethod
    def _workflow_waiting_for_child_approval(run_result: dict[str, Any]) -> bool:
        if str(run_result.get("status") or "") != "approval_required":
            return False
        pending = run_result.get("pending_approval") if isinstance(run_result.get("pending_approval"), dict) else {}
        if pending.get("tool"):
            return False
        return any(
            isinstance(event, dict)
            and str(event.get("event") or "") == "workflow.run.approval_required"
            and bool(str(event.get("child_run_id") or "").strip())
            for event in run_result.get("timeline") or []
        )

    @staticmethod
    def _workflow_child_approval_context(run_result: dict[str, Any], service: Any | None = None) -> dict[str, Any]:
        if not ChatAPI._workflow_waiting_for_child_approval(run_result):
            return {}
        timeline = [event for event in (run_result.get("timeline") or []) if isinstance(event, dict)]
        for event in reversed(timeline):
            if str(event.get("event") or "") != "workflow.run.approval_required":
                continue
            child_run_id = str(event.get("child_run_id") or "").strip()
            if not child_run_id:
                continue
            context = {
                "child_run_id": child_run_id,
                "workflow_node_id": str(event.get("workflow_node_id") or "").strip(),
                "workflow_node_kind": str(event.get("workflow_node_kind") or "").strip(),
                "workflow_node_label": str(event.get("workflow_node_label") or event.get("detail") or "").strip(),
                "child_name": "",
                "tool": "",
            }
            if service is not None:
                try:
                    child = service.get_run(child_run_id)
                    context["child_name"] = str(
                        child.get("runnable_name")
                        or child.get("runnable_id")
                        or ""
                    ).strip()
                    pending = child.get("pending_approval") if isinstance(child.get("pending_approval"), dict) else {}
                    context["tool"] = str(pending.get("tool") or "").strip()
                    if pending.get("tool"):
                        context["pending_approval"] = dict(pending)
                except Exception:
                    logger.debug("读取 Workflow 等待审批子 Run 失败: %s", child_run_id, exc_info=True)
            return context
        return {}

    @staticmethod
    def _workflow_child_approval_notice(context: dict[str, str]) -> str:
        if not context:
            return ""
        child_name = str(context.get("child_name") or "").strip()
        node_label = str(context.get("workflow_node_label") or "").strip()
        node_kind = str(context.get("workflow_node_kind") or "").strip()
        tool = str(context.get("tool") or "").strip()
        target = child_name or node_label or str(context.get("child_run_id") or "").strip()
        lines: list[str] = []
        if target:
            lines.append(f"等待对象：{target}")
        if node_label:
            suffix = f"（{node_kind}）" if node_kind else ""
            lines.append(f"Workflow 节点：{node_label}{suffix}")
        if tool:
            lines.append(f"审批工具：{tool}")
        return "\n" + "\n".join(lines) if lines else ""

    @staticmethod
    def _agent_run_progress_from_timeline(sender: dict[str, Any], run_result: dict[str, Any]) -> tuple[str, str]:
        name = str(sender.get("nickname") or sender.get("name") or run_result.get("runnable_name") or "Agent").strip() or "Agent"
        timeline = [event for event in (run_result.get("timeline") or []) if isinstance(event, dict)]
        for event in reversed(timeline):
            event_name = str(event.get("event") or "").strip()
            detail = _compact_preview(str(event.get("detail") or "").strip(), 140)
            if event_name == "agent.run.resumed":
                return "已批准工具调用", f"{name} 正在继续执行当前任务。"
            if event_name == "agent.tool.call":
                tool = detail or "工具"
                return "正在处理工具结果", f"{name} 已调用 {tool}，正在把结果交回模型判断下一步。"
            if event_name == "agent.artifact.write":
                path = detail or "artifact"
                return "已写出运行产物", f"{name} 写出了 {path}，正在继续处理当前任务。"
            if event_name == "agent.model.response":
                if detail and not _looks_like_internal_protocol_preview(detail):
                    return "正在解析模型响应", f"{name} 已收到模型响应：{detail}"
                return "正在解析模型响应", f"{name} 正在读取模型返回，并判断是否需要工具或产物。"
            if event_name == "agent.runtime.compiled":
                return "运行环境已准备", f"{name} 已加载工具、Skill 和工作区策略，正在调用模型。"
            if event_name == "agent.run.started":
                return "Agent 已开始执行", f"{name} 已收到任务，正在准备运行上下文。"
        return "Agent 正在执行", f"{name} 正在继续处理当前任务。"

    @staticmethod
    def _workflow_run_progress_from_timeline(sender: dict[str, Any], run_result: dict[str, Any]) -> tuple[str, str]:
        name = str(sender.get("nickname") or sender.get("name") or run_result.get("runnable_name") or "Workflow").strip() or "Workflow"
        timeline = [event for event in (run_result.get("timeline") or []) if isinstance(event, dict)]
        for event in reversed(timeline):
            event_name = str(event.get("event") or "").strip()
            detail = _compact_preview(str(event.get("detail") or "").strip(), 140)
            if event_name == "workflow.node.agent":
                return "Workflow 正在执行 Agent", f"{name} 已进入 {detail or 'Agent 节点'}，正在等待节点结果。"
            if event_name == "workflow.node.artifact":
                return "Workflow 正在写出产物", f"{name} 正在处理 {detail or 'Artifact 节点'}。"
            if event_name == "workflow.node.approval_required":
                return "Workflow 等待审批", f"{name} 需要确认 {detail or '人工审批节点'} 后继续。"
            if event_name == "workflow.run.child_resumed":
                return "Workflow 子 Agent 已继续", f"{detail or '子 Agent'} 已通过审批并继续执行。"
            if event_name == "workflow.run.resumed":
                return "Workflow 已继续", f"{name} 已通过审批并继续后续步骤。"
            if event_name == "workflow.run.started":
                path_labels = [
                    str(item.get("label") or "").strip()
                    for item in event.get("workflow_path") or []
                    if isinstance(item, dict)
                    and str(item.get("kind") or "") not in {"", "start"}
                    and str(item.get("label") or "").strip()
                ]
                plan = " → ".join(path_labels)
                detail = f"{name} 已收到目标，正在按流程执行。"
                if plan:
                    detail = f"执行计划：{_compact_preview(plan, 180)}。{detail}"
                return "Workflow 已开始", detail
        return "Workflow 正在执行", f"{name} 正在继续处理当前流程。"

    @staticmethod
    def _workflow_status_label(status: str) -> str:
        normalized = ChatAPI._normalize_agent_run_status(status)
        if normalized == "completed":
            return "已完成"
        if normalized == "approval_required":
            return "等待审批"
        if normalized == "cancelled":
            return "已取消"
        if normalized == "failed":
            return "执行失败"
        if normalized == "processing":
            return "进行中"
        if normalized == "pending":
            return "等待中"
        return f"状态：{normalized or '未知状态'}"

    @staticmethod
    def _visible_run_artifact_summaries(run_result: dict[str, Any]) -> tuple[int, list[dict[str, str]]]:
        summaries: list[dict[str, str]] = []
        count = 0
        for artifact in run_result.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            kind = str(artifact.get("kind") or "").strip()
            path = str(artifact.get("path") or "").strip()
            if not path or kind == "context":
                continue
            count += 1
            if len(summaries) >= 8:
                continue
            summaries.append(
                {
                    "path": _compact_preview(path, 180),
                    "kind": _compact_preview(kind or "artifact", 80),
                }
            )
        return count, summaries

    @staticmethod
    def _run_execution_evidence_lines(run_result: dict[str, Any], max_events: int = 5) -> list[str]:
        labels = {
            "agent.tool.call": "工具调用",
            "agent.tool.skipped": "工具跳过",
            "agent.tool.denied": "工具拒绝",
            "agent.tool.approval_required": "请求审批",
            "agent.tool.approval_rejected": "审批拒绝",
            "agent.run.failed": "Agent 失败",
            "workflow.node.agent": "Workflow Agent 节点",
            "workflow.node.approval_required": "Workflow 审批",
            "workflow.node.approval_rejected": "Workflow 审批拒绝",
            "workflow.run.failed": "Workflow 失败",
            "workflow.run.cancelled": "Workflow 取消",
        }
        candidates: list[str] = []
        for event in run_result.get("timeline") or []:
            if not isinstance(event, dict):
                continue
            event_name = str(event.get("event") or "").strip()
            is_failure = event_name.endswith(".failed") or event_name.endswith(".cancelled")
            if event_name not in labels and not is_failure:
                continue
            label = labels.get(event_name) or event_name
            detail = _compact_preview(str(event.get("detail") or "").strip(), 100)
            parts = [f"{label}{f'：{detail}' if detail else ''}"]
            status = _compact_preview(str(event.get("status") or "").strip(), 80)
            if status:
                parts.append(f"状态 {status}")
            input_preview = _compact_structured_preview(event.get("input_preview"), 260)
            if input_preview:
                parts.append(f"请求 {input_preview}")
            result_preview = _compact_structured_preview(event.get("result"), 340)
            if result_preview:
                parts.append(f"结果 {result_preview}")
            approval_preview = _compact_structured_preview(event.get("pending_approval"), 260)
            if approval_preview:
                parts.append(f"审批 {approval_preview}")
            child_run_id = _compact_preview(str(event.get("child_run_id") or "").strip(), 120)
            if child_run_id:
                parts.append(f"子 Run {child_run_id}")
            candidates.append("；".join(parts))
        return candidates[-max_events:]

    @staticmethod
    def _append_artifact_notice(content: str, artifact_count: int) -> str:
        notice = f"产物：{artifact_count} 个，见运行详情。"
        body = str(content or "").strip()
        if not body:
            return notice
        if notice in body:
            return body
        return f"{body}\n{notice}"

    def _update_agent_run_message_from_result(
        self,
        message_id: str,
        sender: dict[str, Any],
        run_result: dict[str, Any],
        *,
        notify_group_summary: bool = True,
    ) -> None:
        status = self._normalize_agent_run_status(str(run_result.get("status") or "completed"))
        existing_metadata = self._message_metadata(message_id)
        is_workflow_message = existing_metadata.get("runnable_kind") == "workflow" or bool(existing_metadata.get("workflow_status"))
        is_group_message = self._is_group_agent_message(existing_metadata)
        is_delegated_group_agent = is_group_message and bool(existing_metadata.get("delegated_by_task_id"))
        goal = str(existing_metadata.get("group_goal") or existing_metadata.get("delegated_goal") or "").strip()
        content = str(run_result.get("result") or "").strip()
        if status == "approval_required":
            content = self._approval_required_content(sender, run_result, goal=goal if is_group_message else "")
        if not content:
            content = self._run_status_sentence(sender.get("name") or "Agent", status)
        if status in {"processing", "pending"}:
            existing_run_status = str(existing_metadata.get("run_status") or existing_metadata.get("workflow_status") or "").strip()
            if existing_run_status == "approval_required":
                actor_name = sender.get("nickname") or sender.get("name") or ("Workflow" if is_workflow_message else "Agent")
                metadata_update = {
                    "run_status": status,
                    "run_id": run_result.get("run_id") or "",
                    "run_group_id": run_result.get("run_group_id") or "",
                    "pending_approval": {},
                    "workflow_waiting_child_run_id": None,
                    "workflow_waiting_tool": None,
                    "workflow_waiting_node": None,
                    "workflow_waiting_pending_approval": None,
                    "run_progress_title": "审批已通过" if is_workflow_message else "已批准工具调用",
                    "run_progress_detail": (
                        f"{actor_name} 正在继续执行当前流程。"
                        if is_workflow_message
                        else f"{actor_name} 正在继续执行当前任务。"
                    ),
                }
                if is_workflow_message:
                    metadata_update["workflow_status"] = status
                self._session.update_assistant_message(
                    message_id,
                    "",
                    status=MessageStatus.PROCESSING,
                    error=None,
                    metadata=metadata_update,
                )
            return
        agent_report = content
        is_failed = status in {"failed", "cancelled"}
        message_status = self._message_status_for_run_status(status)
        pending_approval = run_result.get("pending_approval") if isinstance(run_result.get("pending_approval"), dict) else {}
        artifact_count, artifact_summaries = self._visible_run_artifact_summaries(run_result)
        artifact_notice_count = artifact_count if status in {"completed", "failed", "cancelled"} else 0
        if is_workflow_message and status in {"completed", "failed", "cancelled"}:
            content = self._workflow_terminal_content(
                sender,
                status,
                agent_report,
                artifact_notice_count=artifact_notice_count,
                node_hint=self._workflow_terminal_node_hint(run_result, status),
            )
        elif is_delegated_group_agent and status in {"completed", "failed", "cancelled"}:
            content = self._group_delegated_agent_terminal_content(
                sender,
                status,
                goal,
                agent_report,
                artifact_notice_count=artifact_notice_count,
                summary_notice=notify_group_summary,
            )
        elif is_group_message and status in {"completed", "failed", "cancelled"}:
            content = self._group_agent_terminal_content(
                sender,
                status,
                agent_report,
                goal,
                artifact_notice_count=artifact_notice_count,
                summary_notice=notify_group_summary,
            )
        metadata_update = {
            "run_status": status,
            "run_id": run_result.get("run_id") or "",
            "run_group_id": run_result.get("run_group_id") or "",
            "pending_approval": pending_approval,
            "run_artifact_count": artifact_count,
            "run_artifacts": artifact_summaries,
        }
        if is_workflow_message:
            metadata_update["workflow_status"] = status
            metadata_update["workflow_waiting_child_run_id"] = None
            metadata_update["workflow_waiting_tool"] = None
            metadata_update["workflow_waiting_node"] = None
            metadata_update["workflow_waiting_pending_approval"] = None
        if is_group_message and status in {"completed", "failed", "cancelled"}:
            metadata_update.update({
                "agent_report": agent_report,
                "agent_report_status": status,
            })
        self._session.update_assistant_message(
            message_id,
            content,
            status=message_status,
            error=content if is_failed else None,
            metadata=metadata_update,
        )
        if notify_group_summary and is_delegated_group_agent and status in {"completed", "failed", "cancelled"}:
            self._maybe_create_group_agent_summary_task(str(existing_metadata.get("delegated_by_task_id") or ""))
        elif notify_group_summary and is_group_message and status in {"completed", "failed", "cancelled"}:
            self._maybe_create_group_direct_agent_summary_task(message_id)

    def _message_metadata(self, message_id: str) -> dict[str, Any]:
        current = next(
            (msg for msg in self._session.get_all_messages() if msg.message_id == message_id),
            None,
        )
        metadata = current.metadata if current is not None and isinstance(current.metadata, dict) else {}
        return dict(metadata)

    @staticmethod
    def _is_group_agent_message(metadata: dict[str, Any]) -> bool:
        return (
            metadata.get("conversation_kind") == "group"
            or bool(metadata.get("delegated_by_task_id"))
            or bool(metadata.get("group_goal"))
        )

    @staticmethod
    def _normalize_agent_run_status(status: str) -> str:
        value = str(status or "").strip()
        return "processing" if value == "running" else value

    @classmethod
    def _group_agent_terminal_content(
        cls,
        sender: dict[str, Any],
        status: str,
        content: str,
        goal: str,
        *,
        artifact_notice_count: int = 0,
        summary_notice: bool = True,
    ) -> str:
        name = str(sender.get("nickname") or sender.get("name") or "Agent").strip() or "Agent"
        if status == "failed":
            intro = f"{name} 执行失败，已把失败原因交给主模型整理。"
        elif status == "cancelled":
            intro = f"{name} 任务已取消，已把当前状态交给主模型整理。" if summary_notice else f"{name} 任务已取消。"
        else:
            intro = f"{name} 已完成任务，已交给主模型整理。"
        lines = [intro]
        goal_text = str(goal or "").strip()
        if goal_text:
            if "\n" in goal_text:
                lines.extend(["任务：", goal_text])
            else:
                lines.append(f"任务：{goal_text}")
        if artifact_notice_count > 0:
            lines.append(f"产物：{artifact_notice_count} 个，见运行详情。")
        body = str(content or "").strip()
        if body:
            lines.extend(["", body])
        return "\n".join(lines)

    @classmethod
    def _workflow_terminal_content(
        cls,
        sender: dict[str, Any],
        status: str,
        content: str,
        *,
        artifact_notice_count: int = 0,
        node_hint: str = "",
    ) -> str:
        name = str(sender.get("nickname") or sender.get("name") or "Workflow").strip() or "Workflow"
        if status == "failed":
            intro = f"{name} 执行失败。"
        elif status == "cancelled":
            intro = f"{name} 已取消。"
        else:
            intro = f"{name} 已完成。"
        body = str(content or "").strip()
        status_prefixes = {
            "completed": (f"{name} 已完成", "Workflow 已完成"),
            "failed": (f"{name} 执行失败", "Workflow 执行失败"),
            "cancelled": (f"{name} 已取消", "Workflow 已取消"),
        }.get(status, ())
        lines = [body] if body and any(body.startswith(prefix) for prefix in status_prefixes) else [intro]
        if node_hint and status in {"failed", "cancelled"}:
            lines.append(node_hint)
        if artifact_notice_count > 0:
            lines.append(f"产物：{artifact_notice_count} 个，见运行详情。")
        if body and lines[0] != body:
            lines.extend(["", body])
        return "\n".join(lines)

    @classmethod
    def _workflow_child_terminal_content(
        cls,
        sender: dict[str, Any],
        status: str,
        content: str,
        *,
        node_label: str = "",
        node_task: str = "",
        artifact_notice_count: int = 0,
    ) -> str:
        name = str(sender.get("nickname") or sender.get("name") or "Agent").strip() or "Agent"
        if status == "failed":
            intro = f"{name} 的 Workflow 节点执行失败。"
        elif status == "cancelled":
            intro = f"{name} 的 Workflow 节点已取消。"
        else:
            intro = f"{name} 已完成 Workflow 节点。"
        lines = [intro]
        if node_label:
            lines.append(f"节点：{node_label}")
        if node_task:
            if "\n" in node_task:
                lines.extend(["任务：", node_task])
            else:
                lines.append(f"任务：{node_task}")
        if artifact_notice_count > 0:
            lines.append(f"产物：{artifact_notice_count} 个，见运行详情。")
        body = str(content or "").strip()
        if body:
            lines.extend(["", body])
        return "\n".join(lines)

    @staticmethod
    def _workflow_terminal_node_hint(run_result: dict[str, Any], status: str) -> str:
        normalized = ChatAPI._normalize_agent_run_status(status)
        if normalized not in {"failed", "cancelled"}:
            return ""
        timeline = [event for event in (run_result.get("timeline") or []) if isinstance(event, dict)]
        event_names = (
            ("workflow.run.failed", "workflow.run.cancelled", "workflow.node.approval_rejected")
            if normalized == "cancelled"
            else ("workflow.run.failed",)
        )
        for event in reversed(timeline):
            if str(event.get("event") or "") not in event_names:
                continue
            label = str(event.get("workflow_node_label") or "").strip()
            kind = str(event.get("workflow_node_kind") or "").strip()
            node_id = str(event.get("workflow_node_id") or "").strip()
            if not label and not node_id:
                continue
            display = label or node_id
            suffix = f"（{kind}）" if kind else ""
            prefix = "取消节点" if normalized == "cancelled" else "失败节点"
            return f"{prefix}：{display}{suffix}"
        return ""

    @classmethod
    def _group_delegated_agent_terminal_content(
        cls,
        sender: dict[str, Any],
        status: str,
        goal: str,
        content: str,
        *,
        artifact_notice_count: int = 0,
        summary_notice: bool = True,
    ) -> str:
        name = str(sender.get("nickname") or sender.get("name") or "Agent").strip() or "Agent"
        if status == "failed":
            intro = f"{name} 执行失败，已把失败原因交给主模型整理。"
        elif status == "cancelled":
            intro = f"{name} 已取消，已把当前状态交给主模型整理。" if summary_notice else f"{name} 已取消。"
        else:
            intro = f"{name} 已完成，并把结果交给主模型汇总。"
        lines = [intro]
        goal_text = str(goal or "").strip()
        if goal_text:
            lines.append(f"任务：{goal_text}")
        if artifact_notice_count > 0:
            lines.append(f"产物：{artifact_notice_count} 个，见运行详情。")
        body = str(content or "").strip()
        if body:
            lines.extend(["", body])
        return "\n".join(lines)

    def _attach_processing_agent_run_metadata(self, message_id: str, content: str, run: dict[str, Any]) -> None:
        current = next(
            (msg for msg in self._session.get_all_messages() if msg.message_id == message_id),
            None,
        )
        if current is None or current.status != MessageStatus.PROCESSING:
            return
        metadata = current.metadata if isinstance(current.metadata, dict) else {}
        run_status = str(metadata.get("run_status") or "processing")
        if run_status not in {"", "pending", "processing"} or current.content != content:
            return
        self._session.update_assistant_message(
            message_id,
            content,
            status=MessageStatus.PROCESSING,
            metadata={
                "run_status": self._normalize_agent_run_status(str(run.get("status") or "processing")),
                "run_id": run.get("run_id") or "",
                "run_group_id": run.get("run_group_id") or "",
            },
        )

    @classmethod
    def _approval_required_content(cls, sender: dict[str, Any], run_result: dict[str, Any], *, goal: str = "") -> str:
        name = str(
            sender.get("nickname")
            or sender.get("name")
            or run_result.get("runnable_name")
            or "Agent"
        ).strip()
        pending = run_result.get("pending_approval") if isinstance(run_result.get("pending_approval"), dict) else {}
        tool = str(pending.get("tool") or "").strip()
        if not tool:
            match = re.search(r"等待审批[:：]\s*(?P<tool>[A-Za-z0-9_.-]+)", str(run_result.get("result") or ""))
            tool = match.group("tool") if match else "tool"
        preview = cls._approval_input_preview_text(tool, pending.get("input_preview"))
        if tool == "workflow.approval":
            lines = [
                f"{name} 需要你确认一个 Workflow 审批节点，批准后会继续当前流程。",
                f"工具：{tool}",
            ]
        else:
            lines = [
                f"{name} 需要你确认一次工具调用，批准后会继续执行当前任务。",
                f"工具：{tool}",
            ]
        goal_text = _compact_preview(goal, 140)
        if goal_text:
            lines.append(f"关联任务：{goal_text}")
        if preview:
            lines.append(f"请求摘要：{preview}")
        return "\n".join(lines)

    @staticmethod
    def _approval_input_preview_text(tool: str, preview: Any) -> str:
        if isinstance(preview, dict):
            command = str(preview.get("command") or "").strip()
            if tool == "terminal.run" and command:
                return f"命令：{_compact_preview(command, 160)}"
            if tool == "workspace.write_patch":
                path = str(preview.get("path") or "").strip()
                content = str(preview.get("content") or "").strip()
                parts: list[str] = []
                if path:
                    parts.append(f"文件：{_compact_preview(path, 120)}")
                if content:
                    parts.append(f"写入内容：{_compact_preview(content, 160)}")
                if parts:
                    return "；".join(parts)
            parts = []
            for key, value in preview.items():
                if key in {"messages", "tool_request", "remaining_tool_requests"}:
                    continue
                text = _compact_preview(value, 80)
                if text:
                    parts.append(f"{key}={text}")
                if len(parts) >= 3:
                    break
            return "；".join(parts)
        if isinstance(preview, list):
            text = _compact_preview(json.dumps(preview, ensure_ascii=False), 180)
            return text
        return _compact_preview(preview, 180)

    @staticmethod
    def _message_status_for_run_status(status: str) -> MessageStatus:
        status = ChatAPI._normalize_agent_run_status(status)
        if status in {"failed", "cancelled"}:
            return MessageStatus.FAILED
        if status in {"approval_required", "processing", "pending"}:
            return MessageStatus.PROCESSING
        return MessageStatus.COMPLETED

    def _bind_session_context(self, kind: str, runnable: dict[str, Any], *, run_group_id: str = "") -> None:
        conversation_kind = kind if kind in {"agent", "workflow", "group"} else "main"
        participants = [self._participant_for_runnable(runnable)] if conversation_kind == "agent" else self._workflow_participants(runnable)
        runnable_name = str(
            runnable.get("nickname")
            or runnable.get("name")
            or runnable.get("id")
            or ""
        )
        self._chat_store().update_session_context(
            self._session.session_id,
            conversation_kind=conversation_kind,
            runnable_id=str(runnable.get("id") or ""),
            runnable_name=runnable_name,
            run_group_id=str(run_group_id or ""),
            participants_json=json.dumps(participants, ensure_ascii=False),
            avatar_url="",
        )

    def _bind_group_session_context(self, context: dict[str, Any], *, run_group_id: str = "") -> None:
        participants = [
            item
            for item in (context.get("participants") or [])
            if isinstance(item, dict)
        ]
        name = str(context.get("runnable_name") or "群组").strip() or "群组"
        self._chat_store().update_session_context(
            self._session.session_id,
            conversation_kind="group",
            runnable_id=str(context.get("runnable_id") or ""),
            runnable_name=name,
            run_group_id=str(run_group_id or context.get("run_group_id") or ""),
            participants_json=json.dumps(participants, ensure_ascii=False),
            avatar_url=self._clean_group_avatar_url(str(context.get("avatar_url") or "")),
        )

    def _set_session_title_from_message(self, content: str) -> None:
        from apps.core.chat_store import make_session_title

        title = make_session_title(content)
        if title:
            self._session.set_session_title(title)

    def _session_context(self, record: Any | None = None) -> dict[str, Any]:
        if record is None:
            try:
                record = self._chat_store().get_session(self._session.session_id)
            except Exception:
                record = None
        kind = str(getattr(record, "conversation_kind", "") or "main")
        if kind not in {"main", "agent", "workflow", "group"}:
            kind = "main"
        participants = self._parse_participants_json(getattr(record, "participants_json", "[]") if record else "[]")
        return {
            "conversation_kind": kind,
            "runnable_id": str(getattr(record, "runnable_id", "") or ""),
            "runnable_name": str(getattr(record, "runnable_name", "") or ""),
            "run_group_id": str(getattr(record, "run_group_id", "") or ""),
            "avatar_url": str(getattr(record, "avatar_url", "") or ""),
            "participants": participants,
        }

    @staticmethod
    def _main_model_goal_text(text: str) -> str:
        value = (text or "").strip()
        if not value.startswith("@"):
            return value
        parsed = ChatAPI._parse_main_model_mention(value)
        if parsed is None:
            return value
        _, remainder = parsed
        return remainder.strip() or value

    @staticmethod
    def _entrypoint_planning_text(
        text: str,
        metadata: dict[str, Any] | None,
    ) -> str:
        value = (text or "").strip()
        if not isinstance(metadata, dict):
            return value
        context = " ".join(str(metadata.get("entrypoint_planning_context") or "").split()).strip()
        if not context:
            return value
        if len(context) > _ENTRYPOINT_PLANNING_CONTEXT_MAX_CHARS:
            context = context[:_ENTRYPOINT_PLANNING_CONTEXT_MAX_CHARS].rstrip()
        if contains_sensitive_text(context):
            return value
        return context

    @staticmethod
    def _compact_participant_detail(value: Any, *, max_chars: int = 120) -> str:
        text = " ".join(str(value or "").split()).strip()
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars].rstrip()}..."

    @staticmethod
    def _participant_tool_policy(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        allowed_tools = value.get("allowed_tools") if isinstance(value.get("allowed_tools"), list) else []
        approvals = value.get("approval_required") if isinstance(value.get("approval_required"), dict) else {}
        return {
            "allowed_tools": [str(tool) for tool in allowed_tools if str(tool)],
            "approval_required": {
                str(tool): bool(required)
                for tool, required in approvals.items()
                if str(tool)
            },
        }

    @staticmethod
    def _participant_tool_label(tool: str) -> str:
        labels = {
            "workspace.list": "列文件",
            "workspace.read": "读文件",
            "workspace.write_patch": "写补丁",
            "terminal.run": "终端",
            "artifact.write": "产物",
        }
        label = labels.get(tool, "")
        return f"{label}({tool})" if label else ChatAPI._compact_participant_detail(tool, max_chars=48)

    @staticmethod
    def _participant_tool_policy_details(participant: dict[str, Any]) -> tuple[str, str]:
        policy = ChatAPI._participant_tool_policy(participant.get("tool_policy"))
        allowed = [tool for tool in policy.get("allowed_tools", []) if str(tool)]
        if not allowed:
            return "", ""
        tool_text = "、".join(ChatAPI._participant_tool_label(tool) for tool in allowed)
        allowed_set = set(allowed)
        approval_required = policy.get("approval_required") or {}
        approvals = [
            tool
            for tool in allowed
            if tool in allowed_set and approval_required.get(tool)
        ]
        approval_text = "、".join(approvals)
        return (
            ChatAPI._compact_participant_detail(tool_text, max_chars=180),
            ChatAPI._compact_participant_detail(approval_text, max_chars=120),
        )

    @staticmethod
    def _participant_context_line(participant: dict[str, Any]) -> str:
        kind = str(participant.get("kind") or "").strip()
        display_name = str(
            participant.get("nickname")
            or participant.get("name")
            or participant.get("id")
            or ""
        ).strip()
        if not display_name:
            return ""
        role = {
            "main": "主模型",
            "agent": "Agent",
            "workflow": "Workflow",
        }.get(kind, kind or "成员")
        details = [role]
        full_name = str(participant.get("name") or "").strip()
        if full_name and full_name != display_name:
            details.append(full_name)
        line = f"- {display_name}（{'；'.join(details)}）"
        capability_details: list[str] = []
        category = ChatAPI._compact_participant_detail(participant.get("category"), max_chars=40)
        if category and category != "main":
            capability_details.append(f"类别：{category}")
        output_contract = ChatAPI._compact_participant_detail(participant.get("output_contract"), max_chars=40)
        if output_contract:
            capability_details.append(f"交付：{output_contract}")
        description = ChatAPI._compact_participant_detail(participant.get("description"), max_chars=160)
        if description:
            capability_details.append(f"职责：{description}")
        tool_text, approval_text = ChatAPI._participant_tool_policy_details(participant)
        if tool_text:
            capability_details.append(f"工具：{tool_text}")
        if approval_text:
            capability_details.append(f"审批：{approval_text}")
        if capability_details:
            line = f"{line} - {'；'.join(capability_details)}"
        return line

    def _with_group_context_for_main_model(self, task_description: str, context: dict[str, Any]) -> str:
        if context.get("conversation_kind") != "group":
            return task_description
        participants = [
            item
            for item in (context.get("participants") or [])
            if isinstance(item, dict)
        ]
        lines = [line for line in (self._participant_context_line(item) for item in participants) if line]
        if not lines:
            return task_description
        member_lines = "\n".join(lines)
        note = (
            f"{_GROUP_CONTEXT_MARKER}\n"
            "当前会话是群组，群成员包括：\n"
            f"{member_lines}\n"
            "当用户没有 @ 指定其他成员时，用户正在对你（主模型/Yachiyo）说话；你可以直接回答，也可以作为团队调度者拆分任务。\n"
            "当用户提到“群里”“群组里”的其他模型或 Agent 时，请只基于上述成员理解，不能派给不在群里的 Agent。\n"
            "派发时请根据每个 Agent 的类别、职责、工具权限、审批边界和交付偏好选择最合适的成员；除非任务确实需要多角色协作，不要默认派给所有 Agent。\n"
            "如果你决定把任务交给群内 Agent，请先用自然语言说明你的计划，然后附加一个机器可读 native 派活 JSON，格式如下：\n"
            '{"tool":"oha.group_dispatch","input":{"tasks":[{"kind":"agent","target":"群成员昵称或名称","goal":"完整、可执行、不可省略的任务说明"}]}}\n'
            "可以一次派给多个 Agent，但每个 goal 都要独立完整，不能用“同上”“继续”等省略说法。\n"
            "被派出的 Agent 会在群聊里发布接收任务、执行结果、失败原因或待审批内容；你不要把派活 JSON 当作给用户阅读的正文。"
        )
        base = (task_description or "").strip()
        return f"{base}\n\n{note}" if base else note

    @staticmethod
    def _is_group_followup_task_description(task_description: str) -> bool:
        return _has_any_marker(str(task_description or ""), _GROUP_FOLLOWUP_MARKERS)

    @staticmethod
    def _group_followup_ack_content() -> str:
        return "收到补充，我会把它纳入当前群组任务的最终整理，不会另起派发。"

    def _with_group_followup_context(
        self,
        task_description: str,
        metadata: dict[str, Any],
    ) -> str:
        if not metadata:
            return task_description
        target_task_ids = [
            str(item or "").strip()
            for item in metadata.get("group_followup_for_task_ids", [])
            if str(item or "").strip()
        ] if isinstance(metadata.get("group_followup_for_task_ids"), list) else []
        target_agent_message_ids = [
            str(item or "").strip()
            for item in metadata.get("group_followup_for_agent_message_ids", [])
            if str(item or "").strip()
        ] if isinstance(metadata.get("group_followup_for_agent_message_ids"), list) else []
        if not target_task_ids and not target_agent_message_ids:
            return task_description
        target_lines: list[str] = []
        if target_task_ids:
            target_lines.append(f"关联主模型派发任务：{', '.join(target_task_ids)}")
        if target_agent_message_ids:
            target_lines.append(f"关联 Agent 消息：{', '.join(target_agent_message_ids)}")
        note_lines = [
            _GROUP_FOLLOWUP_MARKER,
            "这条用户消息是对当前正在执行、待审批或等待汇总的群组 Agent 批次的补充，不是新目标。",
            "不要派发新的 Agent 任务，不要输出 oha.group_dispatch / oha_group_dispatch 或任何机器可读派活 JSON。",
            "你只需要简短确认已收到；最终整理时会把这条补充并入当前批次。",
            *target_lines,
        ]
        base = (task_description or "").strip()
        note = "\n".join(note_lines)
        return f"{base}\n\n{note}" if base else note

    def _with_group_context_for_agent_upstream(
        self,
        upstream: str,
        context: dict[str, Any],
        participant: dict[str, Any],
    ) -> str:
        if context.get("conversation_kind") != "group":
            return upstream
        participants = [
            item
            for item in (context.get("participants") or [])
            if isinstance(item, dict)
        ]
        lines = [line for line in (self._participant_context_line(item) for item in participants) if line]
        name = str(participant.get("nickname") or participant.get("name") or "Agent").strip() or "Agent"
        member_lines = "\n".join(lines) if lines else "- 群成员信息暂不可用"
        note = (
            f"{_GROUP_AGENT_UPSTREAM_MARKER}\n"
            f"当前任务来自群聊，你在群内身份是：{name}。\n"
            "请把输出写成可以直接发到群里的进度、结果、失败原因或待审批说明；不要把过程省略成只有一句“完成”。\n"
            "如果需要用户批准工具调用，请明确写出工具名称、为什么需要、将要执行/读取/修改的关键输入摘要。\n"
            "当前群成员包括：\n"
            f"{member_lines}"
        )
        base = (upstream or "").strip()
        return f"{base}\n\n{note}" if base else note

    def _current_session_has_messages(self) -> bool:
        try:
            return bool(self._chat_store().load_messages(self._session.session_id, limit=1))
        except Exception:
            return self._session.message_count() > 0

    def _chat_upstream_context(self, limit: int = 12) -> str:
        messages = self._session.get_messages(limit)
        lines: list[str] = []
        for msg in messages:
            text = " ".join(str(msg.content or "").split())
            if not text:
                continue
            label = "系统"
            if msg.role == MessageRole.USER:
                label = "用户"
            elif msg.role == MessageRole.ASSISTANT:
                sender = (msg.metadata or {}).get("sender") if isinstance(msg.metadata, dict) else {}
                label = str((sender or {}).get("nickname") or (sender or {}).get("name") or "Yachiyo")
            lines.append(f"{label}: {_compact_preview(text, 180)}")
        return "\n".join(lines[-limit:])

    @staticmethod
    def _participant_for_runnable(runnable: dict[str, Any] | None) -> dict[str, Any]:
        if not runnable:
            return {}
        kind = str(runnable.get("kind") or "agent")
        participant = {
            "kind": kind,
            "id": str(runnable.get("id") or ""),
            "name": str(runnable.get("name") or runnable.get("id") or ""),
        }
        if runnable.get("nickname"):
            participant["nickname"] = str(runnable.get("nickname") or "")
        if runnable.get("description"):
            participant["description"] = str(runnable.get("description") or "")
        if runnable.get("avatar_url"):
            participant["avatar_url"] = str(runnable.get("avatar_url") or "")
        if runnable.get("category"):
            participant["category"] = str(runnable.get("category") or "")
        if runnable.get("output_contract"):
            participant["output_contract"] = str(runnable.get("output_contract") or "")
        tool_policy = ChatAPI._participant_tool_policy(runnable.get("tool_policy"))
        if tool_policy:
            participant["tool_policy"] = tool_policy
        if kind == "workflow":
            participant["participants"] = ChatAPI._workflow_participants(runnable)
        return participant

    @staticmethod
    def _workflow_participants(runnable: dict[str, Any] | None) -> list[dict[str, Any]]:
        participants = (runnable or {}).get("participants") or []
        if not isinstance(participants, list):
            return []
        return [
            ChatAPI._participant_for_runnable(item)
            for item in participants
            if isinstance(item, dict)
        ]

    @staticmethod
    def _main_model_sender() -> dict[str, Any]:
        return {
            "kind": "main",
            "id": "main",
            "name": "Yachiyo",
            "nickname": "月見八千代",
        }

    def _main_model_sender_from_runtime(self) -> dict[str, Any]:
        assistant = getattr(getattr(self._runtime, "config", None), "assistant", None)
        sender: dict[str, Any] = {
            "kind": "main",
            "id": "main",
            "name": str(getattr(assistant, "agent_name", "") or "Yachiyo"),
            "nickname": str(getattr(assistant, "agent_nickname", "") or "月見八千代"),
        }
        avatar_path = str(getattr(assistant, "agent_avatar_path", "") or "")
        if avatar_path:
            sender["avatar_path"] = avatar_path
        return sender

    @staticmethod
    def _parse_participants_json(value: str | None) -> list[dict[str, Any]]:
        if not value:
            return []
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _parse_main_model_mention(text: str) -> tuple[str, str] | None:
        value = (text or "").strip()
        match = re.search(r"(^|[\s，。！？、；;,.!?])@(?P<body>.+)$", value.splitlines()[0] if value else "")
        if not match:
            return None
        body = match.group("body").lstrip()
        body_lower = body.lower()
        for alias in sorted(_MAIN_MODEL_ALIASES, key=len, reverse=True):
            alias_lower = alias.lower()
            if body_lower == alias_lower:
                return alias, ""
            if not body_lower.startswith(alias_lower):
                continue
            remainder = body[len(alias):]
            if remainder and remainder[0] not in _MAIN_MODEL_ALIAS_SEPARATORS:
                continue
            return alias, remainder.lstrip(" \t\r\n:：,，、;；")
        return None

    @staticmethod
    def _has_chat_mention(text: str) -> bool:
        value = (text or "").strip()
        if not value:
            return False
        return bool(re.search(r"(^|[\s，。！？、；;,.!?])@.+", value.splitlines()[0]))

    @staticmethod
    def _clean_group_avatar_url(value: str) -> str:
        return " ".join(str(value or "").split()).strip()[:2_000_000]

    @staticmethod
    def _group_name_from_participants(participants: list[dict[str, Any]]) -> str:
        participant_names: list[str] = []
        for item in participants:
            display_name = str(item.get("nickname") or item.get("name") or "").strip()
            if display_name:
                participant_names.append(display_name)
        return "、".join(participant_names) or "新群组"

    def _group_participants_from_ids(self, participant_ids: list[str] | None) -> tuple[list[dict[str, Any]], str]:
        clean_ids: list[str] = []
        seen: set[str] = set()
        for raw_id in participant_ids or []:
            participant_id = str(raw_id or "").strip()
            if not participant_id or participant_id == "main" or participant_id in seen:
                continue
            seen.add(participant_id)
            clean_ids.append(participant_id)

        if not clean_ids:
            return [], "请选择至少一个 Agent"

        service = self._agent_runtime_service()
        participants = [self._main_model_sender_from_runtime()]
        try:
            for participant_id in clean_ids:
                runnable = service.resolve_runnable(runnable_id=participant_id)
                if runnable is None or runnable.get("kind") != "agent":
                    return [], "群组成员必须是已启用的 Agent"
                if not runnable.get("enabled", True):
                    return [], "群组成员包含已停用 Agent"
                participants.append(self._participant_for_runnable(runnable))
        except AgentRuntimeError as exc:
            return [], redact_api_error_text(exc)
        return participants, ""

    def create_group_session(
        self,
        *,
        name: str = "",
        avatar_url: str = "",
        participant_ids: list[str] | None = None,
    ) -> Dict[str, Any]:
        """Create a manual group chat session with the main model and selected agents."""
        participants, error = self._group_participants_from_ids(participant_ids)
        if error:
            return {"ok": False, "error": error}

        group_name = " ".join(str(name or "").split()).strip()
        if not group_name:
            group_name = self._group_name_from_participants(participants)

        start_new_session = getattr(self._runtime, "start_new_session", None)
        if callable(start_new_session):
            start_new_session()
        else:
            self._session.clear()

        self._session.set_session_title(group_name)
        self._chat_store().update_session_context(
            self._session.session_id,
            conversation_kind="group",
            runnable_id="",
            runnable_name=group_name,
            run_group_id="",
            participants_json=json.dumps(participants, ensure_ascii=False),
            avatar_url=self._clean_group_avatar_url(avatar_url),
        )
        context = self._session_context()
        return {
            "ok": True,
            "session_id": self._session.session_id,
            "session_context": context,
        }

    def update_group_session(
        self,
        session_id: str,
        *,
        name: str = "",
        avatar_url: str = "",
        participant_ids: list[str] | None = None,
    ) -> Dict[str, Any]:
        """Update an existing manual group chat session profile."""
        session_id = str(session_id or "").strip()
        if not session_id:
            return {"ok": False, "error": "session_id 不能为空"}

        store = self._chat_store()
        stored = store.get_session(session_id)
        if stored is None:
            return {"ok": False, "error": "群组不存在"}
        if stored.conversation_kind != "group":
            return {"ok": False, "error": "只能修改手动群组"}

        participants, error = self._group_participants_from_ids(participant_ids)
        if error:
            return {"ok": False, "error": error}

        group_name = " ".join(str(name or "").split()).strip() or self._group_name_from_participants(participants)
        clean_avatar_url = self._clean_group_avatar_url(avatar_url)
        store.update_session_title(session_id, group_name)
        store.update_session_context(
            session_id,
            conversation_kind="group",
            runnable_id=stored.runnable_id,
            runnable_name=group_name,
            run_group_id=stored.run_group_id,
            participants_json=json.dumps(participants, ensure_ascii=False),
            avatar_url=clean_avatar_url,
        )
        context = self._session_context(store.get_session(session_id))
        return {
            "ok": True,
            "session_id": session_id,
            "session_context": context,
        }

    def retry_message(
        self,
        message_id: str,
        *,
        client_message_id: str = "",
    ) -> Dict[str, Any]:
        """Serialize one idempotent retry through its commit boundary."""
        try:
            idempotency_key = self._normalize_client_message_id(client_message_id)
        except AgentRuntimeError as exc:
            return self._not_committed_send_response(
                {"error": redact_api_error_text(exc)},
                "",
            )
        if not idempotency_key:
            return self._retry_message_once(message_id, client_message_id="")

        lock_key = (
            id(self._chat_store()),
            self._session.session_id,
            idempotency_key,
        )
        lock = _SEND_MESSAGE_SINGLEFLIGHT_LOCKS[
            hash(lock_key) % len(_SEND_MESSAGE_SINGLEFLIGHT_LOCKS)
        ]
        with lock:
            return self._retry_message_once(
                message_id,
                client_message_id=idempotency_key,
            )

    def _retry_message_once(
        self,
        message_id: str,
        *,
        client_message_id: str,
    ) -> Dict[str, Any]:
        """重新发送当前会话中的失败消息，复用已保存的附件文件。"""
        message_id = str(message_id or "").strip()
        if not message_id:
            response = {"ok": False, "error": "message_id 不能为空"}
            return (
                self._not_committed_send_response(response, client_message_id)
                if client_message_id
                else response
            )

        try:
            existing_response = self._idempotent_message_response(client_message_id)
            if (
                existing_response is not None
                and not self._client_message_requires_task_resume(client_message_id)
            ):
                return existing_response

            self._sync_task_status_to_messages()
            target = next(
                (msg for msg in self._session.get_all_messages() if msg.message_id == message_id),
                None,
            )
            delegated_retry = self._retry_delegated_agent_message(
                target,
                client_message_id=client_message_id,
            )
            if delegated_retry is not None:
                if not client_message_id:
                    return delegated_retry
                if delegated_retry.get("ok") is False:
                    return self._not_committed_send_response(
                        delegated_retry,
                        client_message_id,
                    )
                return self._accepted_send_response(
                    delegated_retry,
                    client_message_id,
                )

            source = self._find_retry_source_message(message_id)
            if source is None:
                response = {"ok": False, "error": "没有找到可重试的失败消息"}
                return (
                    self._not_committed_send_response(response, client_message_id)
                    if client_message_id
                    else response
                )

            saved_attachments = [dict(attachment) for attachment in source.attachments or []]
            missing_attachments = self._missing_retry_attachments(saved_attachments)
            if missing_attachments:
                response = {
                    "ok": False,
                    "error": f"附件缓存不存在，无法重试：{', '.join(missing_attachments)}",
                }
                return (
                    self._not_committed_send_response(response, client_message_id)
                    if client_message_id
                    else response
                )

            if saved_attachments and self._should_enforce_image_capability():
                image_input = get_native_image_input_capability()
                if image_input.get("can_attach_images") is False:
                    response = {
                        "ok": False,
                        "error": str(image_input.get("reason") or "当前 Native Agent 模型暂不支持图片输入"),
                        "image_input": image_input,
                    }
                    return (
                        self._not_committed_send_response(response, client_message_id)
                        if client_message_id
                        else response
                    )

            text = (source.content or "").strip()
            if not text and saved_attachments:
                text = "请识别并分析这张图片。"
            if not text:
                response = {"ok": False, "error": "原消息内容为空，无法重试"}
                return (
                    self._not_committed_send_response(response, client_message_id)
                    if client_message_id
                    else response
                )

            unavailable_reason = user_task_unavailable_reason(self._runtime)
            if unavailable_reason:
                response = self._unavailable_response(unavailable_reason)
                return (
                    self._not_committed_send_response(response, client_message_id)
                    if client_message_id
                    else response
                )

            retry_metadata = self._with_client_message_id(
                {
                    "retry_of_message_id": message_id,
                    "chat_delivery_requires_task": True,
                },
                client_message_id,
            )
            new_message_id = self._store_user_message(
                text,
                saved_attachments,
                client_message_id=client_message_id,
                metadata=retry_metadata,
            )
            task = self._state.create_task(
                task_type=TaskType.GENERAL,
                description=text,
                attachments=saved_attachments,
                chat_session_id=self._session.session_id,
            )
            self._session.link_message_to_task(new_message_id, task.task_id)

            logger.info(
                "消息已重试: source_message_id=%s, message_id=%s, task_id=%s, attachments=%d",
                message_id,
                new_message_id,
                task.task_id,
                len(saved_attachments),
            )
            response = {
                "ok": True,
                "message_id": new_message_id,
                "source_message_id": message_id,
                "task_id": task.task_id,
                "status": "pending",
                "attachments": self._serialize_attachments(saved_attachments),
            }
            return (
                self._accepted_send_response(response, client_message_id)
                if client_message_id
                else response
            )
        except Exception as exc:
            logger.error("重试消息失败: %s", exc)
            response = {"ok": False, "error": redact_api_error_text(exc)}
            return (
                self._not_committed_send_response(response, client_message_id)
                if client_message_id
                else response
            )

    def _retry_delegated_agent_message(
        self,
        target: ChatMessage | None,
        *,
        client_message_id: str = "",
    ) -> Dict[str, Any] | None:
        if target is None or target.role != MessageRole.ASSISTANT or target.status != MessageStatus.FAILED:
            return None
        metadata = target.metadata if isinstance(target.metadata, dict) else {}
        if not metadata.get("delegated_by_task_id"):
            return None
        if str(metadata.get("runnable_kind") or "") != "agent":
            return None

        runnable_id = str(metadata.get("runnable_id") or "").strip()
        user_goal = str(metadata.get("delegated_goal") or "").strip()
        if not runnable_id or not user_goal:
            return {"ok": False, "error": "这条 Agent 消息缺少可重试的派活信息"}

        if client_message_id:
            for message in reversed(self._session.get_all_messages()):
                candidate_metadata = (
                    message.metadata if isinstance(message.metadata, dict) else {}
                )
                if not (
                    message.role == MessageRole.ASSISTANT
                    and str(candidate_metadata.get("client_message_id") or "")
                    == client_message_id
                    and str(candidate_metadata.get("retry_of_message_id") or "")
                    == target.message_id
                ):
                    continue
                run_id = str(candidate_metadata.get("run_id") or "").strip()
                if not run_id:
                    continue
                status = (
                    message.status.value
                    if isinstance(message.status, MessageStatus)
                    else str(message.status)
                )
                return {
                    "ok": True,
                    "runnable_command": True,
                    "message_id": message.message_id,
                    "assistant_message_id": message.message_id,
                    "assistant_message_ids": [message.message_id],
                    "task_id": "",
                    "status": status,
                    "run_id": run_id,
                    "run_group_id": str(candidate_metadata.get("run_group_id") or ""),
                    "run_status": str(candidate_metadata.get("run_status") or status),
                    "agent_run_id": run_id,
                    "idempotent": True,
                }

        context = self._session_context()
        # A user retry is another root request. Persisted message metadata is
        # display context and must never authorize attachment to an old Group.
        run_group_id = ""
        service = self._agent_runtime_service()
        try:
            runnable = service.resolve_runnable(runnable_id=runnable_id)
        except AgentRuntimeError as exc:
            return {"ok": False, "error": redact_api_error_text(exc)}
        if runnable is None or runnable.get("kind") != "agent":
            return {"ok": False, "error": "没有找到可重试的 Agent"}

        sender = self._participant_for_runnable(runnable)
        initial_content = ""
        assistant_id = self._session.add_assistant_message(
            initial_content,
            metadata={
                "sender": sender,
                "runnable_kind": "agent",
                "runnable_id": runnable_id,
                "run_group_id": run_group_id,
                "run_status": "processing",
                "conversation_kind": "group" if context.get("conversation_kind") == "group" else "",
                "group_goal": user_goal if context.get("conversation_kind") == "group" else "",
                "delegated_by_task_id": metadata.get("delegated_by_task_id") or "",
                "delegated_goal": user_goal,
                "retry_of_message_id": target.message_id,
                **({"client_message_id": client_message_id} if client_message_id else {}),
            },
        )
        self._session.update_assistant_message(
            assistant_id,
            initial_content,
            status=MessageStatus.PROCESSING,
        )
        callback_session_id = self._session.session_id

        def _on_run_complete(run_result: dict[str, Any]) -> None:
            self._with_existing_session(
                callback_session_id,
                lambda: self._update_agent_run_message_from_result(assistant_id, sender, run_result),
            )

        try:
            run = service.create_run_for_runnable_async(
                runnable_id=runnable_id,
                name=str(sender.get("nickname") or sender.get("name") or ""),
                user_goal=user_goal,
                run_group_id=run_group_id,
                upstream=self._with_group_context_for_agent_upstream(
                    self._chat_upstream_context(),
                    context,
                    sender,
                ),
                on_complete=_on_run_complete,
            )
        except AgentRuntimeError as exc:
            agent_report = redact_api_error_text(exc)
            content = self._group_delegated_agent_terminal_content(
                sender,
                "failed",
                user_goal,
                agent_report,
            )
            self._session.update_assistant_message(
                assistant_id,
                content,
                status=MessageStatus.FAILED,
                error=content,
                metadata={
                    "run_status": "failed",
                    "agent_report": agent_report,
                    "agent_report_status": "failed",
                },
            )
            self._maybe_create_group_agent_summary_task(str(metadata.get("delegated_by_task_id") or ""))
            return {"ok": False, "error": content}

        next_run_group_id = str(run.get("run_group_id") or run_group_id)
        self._attach_processing_agent_run_metadata(assistant_id, initial_content, run)
        if next_run_group_id:
            self._bind_group_session_context(context, run_group_id=next_run_group_id)
        return {
            "ok": True,
            "runnable_command": True,
            "message_id": assistant_id,
            "assistant_message_id": assistant_id,
            "assistant_message_ids": [assistant_id],
            "task_id": "",
            "status": "processing",
            "run_id": run["run_id"],
            "run_group_id": next_run_group_id,
            "run_status": "processing",
            "agent_run_id": run["run_id"],
        }

    def _find_retry_source_message(self, message_id: str) -> ChatMessage | None:
        messages = self._session.get_all_messages()
        target_index = next(
            (index for index, msg in enumerate(messages) if msg.message_id == message_id),
            -1,
        )
        if target_index < 0:
            return None

        target = messages[target_index]
        if target.status != MessageStatus.FAILED:
            return None
        if target.role == MessageRole.USER:
            return target

        if target.task_id:
            for msg in reversed(messages[:target_index]):
                if msg.role == MessageRole.USER and msg.task_id == target.task_id:
                    return msg

        for msg in reversed(messages[:target_index]):
            if msg.role == MessageRole.USER:
                return msg
        return None

    @staticmethod
    def _missing_retry_attachments(attachments: list[dict]) -> list[str]:
        missing: list[str] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            if str(attachment.get("kind") or "image") == "audio":
                continue
            path = Path(str(attachment.get("path") or ""))
            if not path or not path.exists() or not path.is_file():
                missing.append(str(attachment.get("name") or attachment.get("id") or "image"))
        return missing

    def _should_enforce_image_capability(self) -> bool:
        runner = getattr(self._runtime, "task_runner", None)
        if runner is None:
            return False
        executor = getattr(runner, "executor", None)
        return bool(execution_capabilities(executor).get("model"))

    def _daily_desktop_requests_can_direct_execute(
        self,
        requests: list[dict[str, Any]],
        text: str = "",
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not requests:
            return False
        if _data_analysis_discovery_request(requests) is not None:
            return _can_direct_execute_data_analysis_discovery(
                requests,
                self._main_chat_default_workdir(),
            )
        if self._can_direct_execute_discovered_app_followup(
            requests,
            text,
            metadata=metadata,
        ):
            return True
        if direct_browser_entrypoint_requests(requests, text):
            return True
        approval_requests = daily_desktop_approval_or_submit_entrypoint_requests(
            requests,
            text=text,
        )
        if approval_requests and daily_desktop_requests_can_complete_without_model(
            approval_requests,
        ):
            return True
        if daily_desktop_requests_can_complete_without_model(requests):
            return True
        if self._can_direct_execute_deferred_observed_ui(requests):
            return True
        return not any(bool(request.get("continue_to_model")) for request in requests)

    @staticmethod
    def _can_direct_execute_deferred_observed_ui(
        requests: list[dict[str, Any]],
    ) -> bool:
        deferred_observations = [
            request
            for request in requests
            if isinstance(request, dict) and bool(request.get("continue_to_model"))
        ]
        if not deferred_observations:
            return False
        for request in deferred_observations:
            tool_name = str(request.get("tool") or "").strip()
            if tool_name not in {"desktop.ui_elements", "desktop.read_ui"}:
                return False
            deferred_tool = str(request.get("deferred_tool") or "").strip()
            if deferred_tool not in _DAILY_DESKTOP_APP_CONTEXT_TOOLS and deferred_tool not in {
                "desktop.click_ui_element",
                "desktop.type_into_ui_element",
                "desktop.safe_click",
                "desktop.safe_type_text",
                "desktop.type_text",
                "desktop.type",
            }:
                return False
            if not isinstance(request.get("deferred_input"), dict):
                return False
            continuation = request.get("deferred_continuation")
            if isinstance(continuation, list) and any(
                isinstance(item, dict) and bool(item.get("continue_to_model"))
                for item in continuation
            ):
                return False
        return True

    def _can_direct_execute_discovered_app_followup(
        self,
        requests: list[dict[str, Any]],
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not text or len(requests) != 1:
            return False
        request = requests[0]
        if str(request.get("tool") or "").strip() != "desktop.list_apps":
            return False
        if not bool(request.get("continue_to_model")):
            return False
        try:
            allowed_tools = main_chat_entrypoint_allowed_tools(
                self._agent_runtime_service(),
                fallback=daily_desktop_allowed_tools(),
            )
        except Exception:
            allowed_tools = daily_desktop_allowed_tools()
        try:
            decision = RuntimePlanner().decision(
                text,
                allowed_tools=allowed_tools,
                metadata=metadata,
            )
            payload = planner_selection_payload(
                decision=decision,
                planner_requests=requests,
                legacy_requests=[],
                selected_requests=requests,
                selected_source="runtime_planner",
                selected_reason="runtime_planner_direct",
                metadata=metadata,
            )
        except Exception:
            logger.debug("Discovered app follow-up direct check failed", exc_info=True)
            return False
        return planner_discovered_app_followup_can_direct_execute(
            payload,
            requests,
            allowed_tools,
        )

    def _main_chat_default_workdir(self) -> Path | None:
        try:
            service = self._agent_runtime_service()
            workspace_policy = service._main_chat_workspace_policy()
        except Exception:
            return None
        if not isinstance(workspace_policy, dict):
            return None
        raw_path = str(workspace_policy.get("default_workdir") or "").strip()
        return Path(raw_path) if raw_path else None

    @staticmethod
    def _should_attach_desktop_snapshot(text: str, saved_attachments: list[dict]) -> bool:
        if saved_attachments:
            return False
        value = (text or "").strip()
        if not value:
            return False
        return bool(_DESKTOP_SNAPSHOT_REQUEST_RE.search(value))

    def _attach_desktop_snapshot_if_needed(
        self,
        text: str,
        saved_attachments: list[dict],
        *,
        should_attach: bool,
    ) -> tuple[str, list[dict], dict[str, Any] | None]:
        """Attach a fresh desktop screenshot when the user explicitly asks Yachiyo to look."""
        if saved_attachments or not should_attach:
            return text, saved_attachments, None

        attachment_id, target_path = allocate_chat_attachment_path(self._session.session_id, ".png")
        proactive_session = is_proactive_chat_session(self._session.session_id)
        source = "proactive_desktop_followup" if proactive_session else "user_requested_desktop_snapshot"
        note_subject = "这条主动关怀追问" if proactive_session else "这条消息"
        try:
            meta = capture_screenshot_to_file(target_path)
            attachment = chat_attachment_record(
                attachment_id,
                target_path,
                kind="image",
                name="主动关怀即时桌面截图.png" if proactive_session else "当前桌面截图.png",
                mime_type="image/png",
            )
            attachment["source"] = source
            _cleanup_attachment_cache({Path(str(attachment["path"]))})
            logger.info(
                "用户请求查看桌面，已附加即时截图: session=%s path=%s (%sx%s)",
                self._session.session_id,
                target_path,
                meta.get("width") if isinstance(meta, dict) else "?",
                meta.get("height") if isinstance(meta, dict) else "?",
            )
            task_description = (
                f"{text}\n\n"
                f"[Yachiyo 已为{note_subject}附加当前桌面截图；"
                "请优先基于附件图片回答用户问题。]"
            )
            return task_description, [attachment], None
        except Exception as exc:
            try:
                target_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("清理按需桌面截图失败: %s", target_path, exc_info=True)
            safe_error = redact_api_error_text(exc)
            logger.warning("按需桌面截图捕获失败: %s", safe_error)
            desktop_snapshot_error: dict[str, Any] = {
                "code": ErrorCode.SCREEN_CAPTURE_PERMISSION_DENIED
                if isinstance(exc, ScreenCapturePermissionError)
                else ErrorCode.ADAPTER_ERROR,
                "message": "屏幕录制权限不足，请在系统设置中授权 Oha-Yachiyo 后重启 Bridge。"
                if isinstance(exc, ScreenCapturePermissionError)
                else "当前无法读取桌面截图。",
                "detail": safe_error,
                "permission_denied": isinstance(exc, ScreenCapturePermissionError),
            }
            task_description = (
                f"{text}\n\n"
                f"[Yachiyo 尝试为{note_subject}捕获当前桌面截图，但失败：{safe_error}。"
                "请向用户说明当前无法读取桌面截图。]"
            )
            return task_description, saved_attachments, desktop_snapshot_error

    def _record_desktop_snapshot_error_activity(self, task_id: str, error: dict[str, Any]) -> None:
        try:
            self._activity_store().record_event(
                event_id=f"{task_id}-desktop-snapshot-error",
                session_id=self._session.session_id,
                task_id=task_id,
                tool_name="desktop_snapshot",
                phase="desktop_snapshot",
                title="无法读取桌面截图",
                detail=str(error.get("detail") or error.get("message") or ""),
                status="failed",
                metadata={"desktop_snapshot_error": error},
            )
        except Exception:
            logger.debug("记录桌面截图失败 activity 失败: task_id=%s", task_id, exc_info=True)

    def get_messages(self, limit: int = 0, anchor_message_id: str = "") -> Dict[str, Any]:
        """获取消息列表，同时同步任务状态到消息

        此方法会检查每条 user 消息关联的任务状态：
          - 任务 COMPLETED → 若无对应 assistant 回复，自动添加
          - 任务 FAILED → 标记消息失败
          - 任务 RUNNING → 更新消息状态为 processing

        消息排序：保证每条 user 消息紧跟其关联的 assistant 回复，
        避免并发任务完成顺序不一致导致消息错位。

        Returns:
            {"ok": True, "session_id": str, "messages": [...], "is_processing": bool}
        """
        try:
            # 同步主模型任务和 Agent/Workflow Run 状态，再刷新持久化快照。
            self._sync_current_session_status()
            self._session.reload_from_store(fail_active_messages=False)
            self._sync_group_agent_summary_parent_statuses()

            anchor_message_id = str(anchor_message_id or "").strip()
            if anchor_message_id:
                messages = self._load_messages_around_anchor(anchor_message_id, limit=limit)
                anchor_found = any(m.message_id == anchor_message_id for m in messages)
                if not anchor_found:
                    messages = self._session.get_messages(limit)
            else:
                messages = self._session.get_messages(limit)
            sorted_msgs = self._sort_messages_by_task(messages)
            task_ids = [m.task_id for m in sorted_msgs if m.task_id]
            activity_by_task = self._activity_events_by_task(task_ids, limit_per_task=5)
            serialized_messages = self._serialize_chat_messages(sorted_msgs, activity_by_task)
            processing_count = self._session_processing_count(self._session.session_id, messages=sorted_msgs)
            approval_count = self._session_approval_count(self._session.session_id, messages=sorted_msgs)
            all_messages = self._session.get_all_messages()
            return {
                "ok": True,
                "session_id": self._session.session_id,
                "session_context": self._session_context(),
                "is_processing": processing_count > 0,
                "processing_count": processing_count,
                "approval_count": approval_count,
                "messages": serialized_messages,
                "token_count": estimate_chat_tokens(all_messages),
                "anchor_message_id": anchor_message_id,
            }

        except Exception as exc:
            logger.error("获取消息列表失败: %s", exc)
            return {"ok": False, "error": redact_api_error_text(exc), "messages": []}

    def _load_messages_around_anchor(self, message_id: str, *, limit: int) -> list[ChatMessage]:
        context = max(20, min(int(limit or 80), 400))
        before = max(10, int(context * 0.65))
        after = max(10, context - before - 1)
        stored_messages = self._chat_store().load_messages_around(
            self._session.session_id,
            message_id,
            before=before,
            after=after,
        )
        return self._stored_messages_to_chat_messages(stored_messages)

    def _stored_messages_to_chat_messages(self, stored_messages: list[Any]) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for stored in stored_messages:
            try:
                role = MessageRole(str(getattr(stored, "role", "")))
                status = MessageStatus(str(getattr(stored, "status", "")))
                created_at = datetime.fromisoformat(str(getattr(stored, "created_at", "")))
            except ValueError:
                logger.debug("跳过无法序列化的聊天消息: %s", getattr(stored, "message_id", ""), exc_info=True)
                continue
            attachments_json = str(getattr(stored, "attachments_json", "") or "[]")
            try:
                parsed_attachments = json.loads(attachments_json)
                attachments = parsed_attachments if isinstance(parsed_attachments, list) else []
            except json.JSONDecodeError:
                attachments = []
            metadata_json = str(getattr(stored, "metadata_json", "") or "{}")
            try:
                parsed_metadata = json.loads(metadata_json)
                metadata = parsed_metadata if isinstance(parsed_metadata, dict) else {}
            except json.JSONDecodeError:
                metadata = {}
            messages.append(
                ChatMessage(
                    message_id=str(getattr(stored, "message_id", "")),
                    role=role,
                    content=str(getattr(stored, "content", "") or ""),
                    status=status,
                    created_at=created_at,
                    task_id=getattr(stored, "task_id", None),
                    error=getattr(stored, "error", None),
                    attachments=attachments,
                    metadata=metadata,
                )
            )
        return messages

    def _serialize_chat_messages(
        self,
        messages: list[ChatMessage],
        activity_by_task: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        serialized_messages = []
        for m in messages:
            show_activity = m.role == MessageRole.ASSISTANT
            activity_events = activity_by_task.get(m.task_id or "", []) if show_activity else []
            serialized_messages.append(
                {
                    "id": m.message_id,
                    "role": m.role.value,
                    "content": m.content,
                    "status": m.status.value,
                    "task_id": m.task_id,
                    "error": m.error,
                    "created_at": m.created_at.isoformat(),
                    "attachments": self._serialize_attachments(m.attachments),
                    "metadata": m.metadata or {},
                    "token_count": estimate_chat_message_tokens(m),
                    "progress_label": self._task_progress_label(m.task_id) if activity_events else "",
                    "activity_events": activity_events,
                }
            )
        return serialized_messages

    @staticmethod
    def _sort_messages_by_task(messages: List[ChatMessage]) -> List[ChatMessage]:
        """按 task 关联重排消息，保证 user 消息紧跟其 assistant 回复。

        算法：遍历消息列表，将 assistant 消息按 task_id 索引。
        输出时，每条 user 消息后立即插入对应 assistant 消息。
        system 消息和无 task_id 的消息保持原始顺序。
        """
        user_task_ids = {
            msg.task_id
            for msg in messages
            if msg.role == MessageRole.USER and msg.task_id
        }

        # 建立 task_id → assistant 消息的映射。只有同页存在 user
        # 消息的 task 才做配对重排；主动关怀这类 assistant-only
        # 消息保持原本时间线位置。若历史库里已经有重复 assistant，
        # 只取最可信的一条，避免 UI 再把脏数据渲染成重复回复。
        assistant_by_task: dict[str, ChatMessage] = {}
        for msg in messages:
            if msg.role == MessageRole.ASSISTANT and msg.task_id in user_task_ids:
                current = assistant_by_task.get(msg.task_id)
                if current is None or ChatAPI._prefer_assistant_for_sort(msg, current):
                    assistant_by_task[msg.task_id] = msg

        result: list[ChatMessage] = []
        inserted_assistant_ids: set[str] = set()

        for msg in messages:
            if msg.role == MessageRole.ASSISTANT and msg.task_id in user_task_ids:
                # assistant 消息由 user 消息触发插入，跳过
                continue
            result.append(msg)
            # user 消息后紧跟其关联的 assistant 回复
            if msg.role == MessageRole.USER and msg.task_id:
                assistant = assistant_by_task.get(msg.task_id)
                if assistant is not None:
                    result.append(assistant)
                    inserted_assistant_ids.add(assistant.message_id)

        # 兜底：分页/limit 截断时 user 可能不在当前列表，
        # 不能丢弃这条 canonical assistant。
        for msg in assistant_by_task.values():
            if msg.message_id not in inserted_assistant_ids:
                result.append(msg)

        return result

    @staticmethod
    def _prefer_assistant_for_sort(candidate: ChatMessage, current: ChatMessage) -> bool:
        status_rank = {
            MessageStatus.COMPLETED: 0,
            MessageStatus.FAILED: 1,
            MessageStatus.PROCESSING: 2,
            MessageStatus.PENDING: 3,
        }
        candidate_rank = status_rank.get(candidate.status, 9)
        current_rank = status_rank.get(current.status, 9)
        if candidate_rank != current_rank:
            return candidate_rank < current_rank
        return candidate.created_at > current.created_at

    def get_attachment_file(self, attachment_id: str) -> Dict[str, Any]:
        """返回聊天附件文件信息，供 HTTP 路由发送预览图。"""
        attachment_id = (attachment_id or "").strip()
        if not attachment_id or not re.fullmatch(r"[a-f0-9]{32}", attachment_id):
            return {"ok": False, "error": "附件 ID 无效"}

        for msg in self._session.get_all_messages():
            for attachment in msg.attachments or []:
                if str(attachment.get("id") or "") != attachment_id:
                    continue
                path = Path(str(attachment.get("path") or ""))
                root = _attachment_root().resolve()
                try:
                    resolved = path.resolve()
                except OSError:
                    return {"ok": False, "error": "附件路径无效"}
                if root not in resolved.parents:
                    return {"ok": False, "error": "附件路径越界"}
                if not resolved.exists() or not resolved.is_file():
                    return {"ok": False, "error": "附件文件不存在"}
                return {
                    "ok": True,
                    "path": str(resolved),
                    "mime_type": str(attachment.get("mime_type") or "image/png"),
                    "name": str(attachment.get("name") or resolved.name),
                }
        return {"ok": False, "error": "附件不存在或不属于当前会话"}

    def _save_attachments(self, attachments: list[dict]) -> list[dict]:
        if not attachments:
            return []
        if len(attachments) > _MAX_CHAT_ATTACHMENTS:
            raise ValueError(f"最多一次发送 {_MAX_CHAT_ATTACHMENTS} 张图片")

        session_dir = _attachment_root() / self._session.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        saved: list[dict] = []
        for index, item in enumerate(attachments, start=1):
            saved.append(self._save_attachment(item, session_dir, index))
        _cleanup_attachment_cache(
            {Path(str(attachment["path"])) for attachment in saved if attachment.get("path")}
        )
        return saved

    def _save_attachment(self, item: dict, session_dir: Path, index: int) -> dict:
        if not isinstance(item, dict):
            raise ValueError("附件格式无效")
        data_url = str(item.get("data_url") or item.get("dataUrl") or "")
        match = _DATA_URL_RE.match(data_url)
        if not match:
            raise ValueError("只支持粘贴图片附件")

        mime_type = match.group(1).lower()
        extension = _IMAGE_EXTENSIONS_BY_MIME.get(mime_type)
        if not extension:
            raise ValueError(f"暂不支持此图片格式：{mime_type}")

        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("图片数据无法解析") from exc

        if not raw:
            raise ValueError("图片内容为空")
        if len(raw) > _MAX_ATTACHMENT_BYTES:
            limit_mb = _MAX_ATTACHMENT_BYTES // (1024 * 1024)
            raise ValueError(f"单张图片不能超过 {limit_mb} MB")

        attachment_id = uuid4().hex
        safe_name = _sanitize_attachment_name(str(item.get("name") or f"image-{index}{extension}"))
        if not Path(safe_name).suffix:
            safe_name += extension
        target = session_dir / f"{attachment_id}{extension}"
        target.write_bytes(raw)
        return {
            "id": attachment_id,
            "kind": "image",
            "name": safe_name,
            "mime_type": mime_type,
            "size": len(raw),
            "path": str(target),
        }

    @staticmethod
    def _serialize_attachments(attachments: list[dict] | None) -> list[dict]:
        result: list[dict] = []
        for attachment in attachments or []:
            if not isinstance(attachment, dict):
                continue
            attachment_id = str(attachment.get("id") or "")
            if not attachment_id:
                continue
            item = {
                "id": attachment_id,
                "kind": str(attachment.get("kind") or "image"),
                "name": str(attachment.get("name") or "image"),
                "mime_type": str(attachment.get("mime_type") or "image/png"),
                "size": int(attachment.get("size") or 0),
                "url": _attachment_public_url(attachment_id),
            }
            if attachment.get("source"):
                item["source"] = str(attachment.get("source") or "")
            if attachment.get("spoken_text"):
                item["spoken_text"] = str(attachment.get("spoken_text") or "")
            result.append(item)
        return result

    def _sync_task_status_to_messages(self, *, notify_group_summary: bool = True) -> None:
        """将任务状态同步到关联的消息

        使用 upsert_assistant_message() 保证幂等：
          - RUNNING: 创建/更新 assistant 占位消息（PROCESSING）
          - COMPLETED: 更新 assistant 消息为最终结果
          - FAILED: 更新 assistant 消息为错误信息
          - CANCELLED: 更新 assistant 消息为取消提示

        同一个 task_id 永远只对应一条 assistant 消息，
        无论此方法被并发调用多少次都不会产生重复。
        """
        self._sync_native_direct_group_tasks(notify_group_summary=notify_group_summary)
        synced_task_ids: set[str] = set()
        current_context = self._session_context()
        for msg in self._session.get_all_messages():
            if msg.role not in (MessageRole.USER, MessageRole.ASSISTANT):
                continue
            if msg.task_id is None:
                continue
            if msg.task_id in synced_task_ids:
                continue
            if msg.status in (MessageStatus.COMPLETED, MessageStatus.FAILED):
                continue

            task = self._state.get_task(msg.task_id)
            if task is None:
                run = self._linked_main_chat_run_for_task(msg.task_id)
                if run:
                    synced_task_ids.add(msg.task_id)
                    self._sync_missing_task_from_durable_run(
                        msg,
                        run,
                        current_context,
                    )
                    continue
                if msg.status in (MessageStatus.PENDING, MessageStatus.PROCESSING):
                    self._session.mark_message_failed(msg.message_id, "任务状态不可恢复")
                continue
            synced_task_ids.add(msg.task_id)

            if task.status == TaskStatus.COMPLETED:
                assistant = self._session.get_assistant_message_for_task(msg.task_id)
                assistant_metadata = assistant.metadata if assistant and isinstance(assistant.metadata, dict) else {}
                is_group_summary_message = bool(
                    assistant_metadata.get("group_agent_summary_for_task_id")
                    or assistant_metadata.get("group_direct_agent_summary_for_message_id")
                )
                if (
                    assistant is not None
                    and assistant_metadata.get("group_dispatch_handled")
                    and not is_group_summary_message
                ):
                    self._session.upsert_assistant_message(
                        task_id=msg.task_id,
                        content=assistant.content,
                        status=assistant.status,
                        error=assistant.error,
                        attachments=assistant.attachments,
                        metadata=assistant_metadata,
                    )
                    continue
                result = task.result or "[任务已完成，无输出]"
                metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
                result = self._sanitize_group_summary_result(result, metadata, current_context)
                self._session.upsert_assistant_message(
                    task_id=msg.task_id,
                    content=result,
                    status=MessageStatus.COMPLETED,
                    metadata=_terminal_or_group_summary_metadata(assistant_metadata, "completed"),
                )

            elif task.status == TaskStatus.FAILED:
                error = task.error or "任务执行失败"
                assistant = self._session.get_assistant_message_for_task(msg.task_id)
                assistant_metadata = assistant.metadata if assistant and isinstance(assistant.metadata, dict) else {}
                self._session.upsert_assistant_message(
                    task_id=msg.task_id,
                    content=f"❌ {error}",
                    status=MessageStatus.FAILED,
                    error=error,
                    metadata=_terminal_or_group_summary_metadata(assistant_metadata, "failed"),
                )

            elif task.status == TaskStatus.CANCELLED:
                error = "任务已取消"
                assistant = self._session.get_assistant_message_for_task(msg.task_id)
                assistant_metadata = assistant.metadata if assistant and isinstance(assistant.metadata, dict) else {}
                self._session.upsert_assistant_message(
                    task_id=msg.task_id,
                    content=f"⚠️ {error}",
                    status=MessageStatus.FAILED,
                    error=error,
                    metadata=_terminal_or_group_summary_metadata(assistant_metadata, "cancelled"),
                )

            elif task.status == TaskStatus.RUNNING:
                if self._sync_main_chat_run_projection_for_running_task(task):
                    continue
                assistant = self._session.get_assistant_message_for_task(msg.task_id)
                if assistant is None:
                    self._session.upsert_assistant_message(
                        task_id=msg.task_id,
                        content="",
                        status=MessageStatus.PROCESSING,
                    )
                elif self._should_hide_group_dispatch_stream(task.description, assistant.content, current_context):
                    visible_content = self._group_dispatch_stream_visible_content(
                        assistant.content,
                        assistant.metadata if isinstance(assistant.metadata, dict) else {},
                    )
                    self._record_group_dispatch_activity(
                        task_id=msg.task_id,
                        title="正在派发群组任务",
                        detail="Yachiyo 正在解析需要交给哪些 Agent。",
                        status="running",
                        event_id=f"{msg.task_id}-group-dispatch-start",
                    )
                    metadata = dict(assistant.metadata or {})
                    metadata.update({
                        "sender": metadata.get("sender") or self._main_model_sender_from_runtime(),
                        "group_dispatch_pending": True,
                        "group_dispatch_stream_visible_content": visible_content,
                    })
                    self._session.upsert_assistant_message(
                        task_id=msg.task_id,
                        content=visible_content,
                        status=MessageStatus.PROCESSING,
                        error=assistant.error,
                        attachments=assistant.attachments,
                        metadata=metadata,
                    )
                elif assistant.status != MessageStatus.PROCESSING:
                    self._session.upsert_assistant_message(
                        task_id=msg.task_id,
                        content=assistant.content,
                        status=MessageStatus.PROCESSING,
                        error=assistant.error,
                    )

        self._sync_group_dispatches_from_completed_tasks(notify_group_summary=notify_group_summary)
        self._sync_group_agent_summary_parent_statuses()

    def _sync_missing_task_from_durable_run(
        self,
        message: ChatMessage,
        run: dict[str, Any],
        current_context: dict[str, Any],
    ) -> bool:
        task_id = str(message.task_id or "").strip()
        if not task_id:
            return False
        status = self._normalize_agent_run_status(str(run.get("status") or ""))
        if status == "approval_required":
            goal = message.content if message.role == MessageRole.USER else ""
            return self._project_main_chat_approval_message(
                task_id=task_id,
                run=run,
                goal=goal,
            ) is not None
        if status in {"processing", "pending"}:
            assistant = self._session.get_assistant_message_for_task(task_id)
            existing_metadata = (
                dict(assistant.metadata or {})
                if assistant and isinstance(assistant.metadata, dict)
                else {}
            )
            metadata = {
                **existing_metadata,
                "run_status": status,
                "run_id": run.get("run_id") or "",
                "run_group_id": run.get("run_group_id") or "",
            }
            if str(existing_metadata.get("run_status") or "") == "approval_required":
                metadata.update({
                    "pending_approval": {},
                    "run_progress_title": "审批已通过",
                    "run_progress_detail": "Yachiyo 正在继续执行当前任务。",
                })
            self._session.upsert_assistant_message(
                task_id=task_id,
                content=assistant.content if assistant is not None else "",
                status=MessageStatus.PROCESSING,
                error=None,
                attachments=assistant.attachments if assistant is not None else None,
                metadata=metadata,
            )
            return True
        if status == "completed":
            assistant = self._session.get_assistant_message_for_task(task_id)
            assistant_metadata = (
                dict(assistant.metadata or {})
                if assistant and isinstance(assistant.metadata, dict)
                else {}
            )
            result = str(run.get("result") or "[任务已完成，无输出]")
            message_metadata = (
                message.metadata if isinstance(message.metadata, dict) else {}
            )
            result = self._sanitize_group_summary_result(
                result,
                message_metadata,
                current_context,
            )
            if message.role == MessageRole.USER:
                self._session.mark_message_completed(message.message_id)
            self._session.upsert_assistant_message(
                task_id=task_id,
                content=result,
                status=MessageStatus.COMPLETED,
                metadata=_terminal_or_group_summary_metadata(
                    assistant_metadata,
                    "completed",
                ),
            )
            return True
        if status == "failed":
            assistant = self._session.get_assistant_message_for_task(task_id)
            assistant_metadata = (
                dict(assistant.metadata or {})
                if assistant and isinstance(assistant.metadata, dict)
                else {}
            )
            error = str(run.get("result") or "任务执行失败")
            if message.role == MessageRole.USER:
                self._session.mark_message_failed(message.message_id, error)
            self._session.upsert_assistant_message(
                task_id=task_id,
                content=f"❌ {error}",
                status=MessageStatus.FAILED,
                error=error,
                metadata=_terminal_or_group_summary_metadata(
                    assistant_metadata,
                    "failed",
                ),
            )
            return True
        if status == "cancelled":
            assistant = self._session.get_assistant_message_for_task(task_id)
            assistant_metadata = (
                dict(assistant.metadata or {})
                if assistant and isinstance(assistant.metadata, dict)
                else {}
            )
            error = "任务已取消"
            if message.role == MessageRole.USER:
                self._session.mark_message_failed(message.message_id, error)
            self._session.upsert_assistant_message(
                task_id=task_id,
                content=f"⚠️ {error}",
                status=MessageStatus.FAILED,
                error=error,
                metadata=_terminal_or_group_summary_metadata(
                    assistant_metadata,
                    "cancelled",
                ),
            )
            return True
        return False

    def _sync_main_chat_run_projection_for_running_task(self, task: Any) -> bool:
        """Project native main-chat Run approval state onto the Task chat message.

        Task remains the product-level lifecycle source of truth.  While a Task
        is RUNNING, the linked Native Run may be paused for tool approval; this
        keeps ChatSession and ActivityStore aligned with that user-visible wait.
        """
        task_id = str(getattr(task, "task_id", "") or "").strip()
        if not task_id:
            return False
        run = self._linked_main_chat_run_for_task(task_id)
        if not run:
            return False
        status = self._normalize_agent_run_status(str(run.get("status") or ""))
        assistant = self._session.get_assistant_message_for_task(task_id)
        existing_metadata = dict(assistant.metadata or {}) if assistant and isinstance(assistant.metadata, dict) else {}
        if status in {"completed", "failed", "cancelled"}:
            # Approval routes settle the durable Run independently of TaskRunner.
            # Polling must project that same linked Run onto the in-memory Task.
            if str(run.get("task_id") or task_id) != task_id:
                return False
            run_goal = run.get("user_goal")
            if run_goal is not None and str(run_goal) != str(getattr(task, "description", "") or ""):
                return False
            if getattr(task, "status", None) != TaskStatus.RUNNING:
                return False
            summary = str(run.get("result") or "")
            error = None
            if status != "completed":
                error = "任务已取消" if status == "cancelled" else summary or "任务执行失败"
            terminal_status = {
                "completed": TaskStatus.COMPLETED,
                "failed": TaskStatus.FAILED,
                "cancelled": TaskStatus.CANCELLED,
            }[status]
            try:
                self._state.update_task_status(
                    task_id,
                    terminal_status,
                    result=summary if status == "completed" else None,
                    error=error,
                )
            except (KeyError, ValueError):
                # Another observer may already have settled or removed this Task.
                return False
            if assistant is not None:
                self._sync_missing_task_from_durable_run(assistant, run, self._session_context())
            return True
        if status == "approval_required":
            content = self._project_main_chat_approval_message(
                task_id=task_id,
                run=run,
                goal=str(getattr(task, "description", "") or ""),
            )
            if content is None:
                return False
            self._record_main_chat_approval_activity(task, run, content)
            return True
        if status in {"processing", "pending"} and str(existing_metadata.get("run_status") or "") == "approval_required":
            metadata = {
                **existing_metadata,
                "run_status": status,
                "run_id": run.get("run_id") or "",
                "run_group_id": run.get("run_group_id") or "",
                "pending_approval": {},
                "run_progress_title": "审批已通过",
                "run_progress_detail": "Yachiyo 正在继续执行当前任务。",
            }
            self._session.upsert_assistant_message(
                task_id=task_id,
                content="",
                status=MessageStatus.PROCESSING,
                metadata=metadata,
            )
            return True
        return False

    def _project_main_chat_approval_message(
        self,
        *,
        task_id: str,
        run: dict[str, Any],
        goal: str,
    ) -> str | None:
        pending = run.get("pending_approval") if isinstance(run.get("pending_approval"), dict) else {}
        if not pending.get("tool"):
            return None
        assistant = self._session.get_assistant_message_for_task(task_id)
        existing_metadata = (
            dict(assistant.metadata or {})
            if assistant and isinstance(assistant.metadata, dict)
            else {}
        )
        sender = (
            existing_metadata.get("sender")
            if isinstance(existing_metadata.get("sender"), dict)
            else self._main_model_sender_from_runtime()
        )
        content = self._approval_required_content(sender, run, goal=goal)
        metadata = {
            **existing_metadata,
            "sender": sender,
            "run_status": "approval_required",
            "run_id": run.get("run_id") or "",
            "run_group_id": run.get("run_group_id") or "",
            "pending_approval": pending,
            "run_progress_title": "等待工具审批",
            "run_progress_detail": content,
        }
        self._session.upsert_assistant_message(
            task_id=task_id,
            content=content,
            status=MessageStatus.PROCESSING,
            metadata=metadata,
        )
        return content

    def _linked_main_chat_run_for_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            service = self._agent_runtime_service()
            get_link = getattr(service, "get_task_run_link", None)
            get_run = getattr(service, "get_run", None)
            if not callable(get_link) or not callable(get_run):
                return None
            link = get_link(task_id)
            run_id = str(link.get("run_id") or "").strip() if isinstance(link, dict) else ""
            if not run_id:
                return None
            run = get_run(run_id)
        except KeyError:
            return None
        except Exception:
            logger.debug("读取主聊天 Native Run 投影失败: %s", task_id, exc_info=True)
            return None
        if not isinstance(run, dict):
            return None
        if str(run.get("kind") or "") not in {"", "main_chat_run"}:
            return None
        return run

    def _record_main_chat_approval_activity(self, task: Any, run: dict[str, Any], content: str) -> None:
        pending = run.get("pending_approval") if isinstance(run.get("pending_approval"), dict) else {}
        tool = str(pending.get("tool") or "").strip()
        if not tool:
            return
        task_id = str(getattr(task, "task_id", "") or "").strip()
        try:
            self._activity_store().record_event(
                event_id=f"{task_id}-main-chat-approval-required",
                session_id=str(getattr(task, "chat_session_id", "") or ""),
                task_id=task_id,
                tool_name=tool,
                phase="tool_start",
                title="等待工具审批",
                detail=content,
                status="approval_required",
                metadata={
                    "run_id": run.get("run_id") or "",
                    "run_group_id": run.get("run_group_id") or "",
                    "run_status": "approval_required",
                    "pending_approval": pending,
                },
            )
        except Exception:
            logger.debug("写入主聊天审批活动失败: %s", task_id, exc_info=True)

    def _sanitize_group_summary_result(
        self,
        result: str,
        metadata: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        if context.get("conversation_kind") != "group":
            return result
        if not metadata.get("group_agent_summary_for_task_id") and not metadata.get("group_direct_agent_summary_for_message_id"):
            return result
        if not self._parse_group_dispatch_directives(result):
            return result
        visible = self._normalize_group_dispatch_intro(self._strip_group_dispatch_payloads(result)).strip()
        notice = "主模型汇总返回了内部派发协议，已隐藏；请参考上方 Agent 结果，或让主模型重新整理。"
        if not visible:
            return notice
        if notice in visible:
            return visible
        return f"{visible}\n\n{notice}"

    def _sync_current_session_status(self, *, notify_group_summary: bool = True) -> None:
        """同步当前会话里的主模型任务和 Agent/Workflow Run 消息状态。"""
        self._sync_task_status_to_messages(notify_group_summary=notify_group_summary)
        self._sync_runnable_run_status_to_messages(notify_group_summary=notify_group_summary)

    def _sync_runnable_run_status_to_messages(self, *, notify_group_summary: bool = True) -> None:
        candidates: list[tuple[ChatMessage, dict[str, Any]]] = []
        for msg in self._session.get_all_messages():
            if msg.role != MessageRole.ASSISTANT:
                continue
            metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            if metadata.get("runnable_kind") not in {"agent", "workflow"}:
                continue
            run_id = str(metadata.get("run_id") or metadata.get("workflow_run_id") or "").strip()
            if not run_id:
                continue
            run_status = str(metadata.get("run_status") or metadata.get("workflow_status") or "").strip()
            if msg.status != MessageStatus.PROCESSING and run_status not in _ACTIVE_RUN_STATUSES:
                continue
            candidates.append((msg, metadata))
        if not candidates:
            return

        try:
            service = self._agent_runtime_service()
        except Exception:
            logger.debug("读取 Run 状态失败", exc_info=True)
            return
        for msg, metadata in candidates:
            if metadata.get("workflow_parent_run_id"):
                # Workflow child messages are synchronized together with their
                # parent so node/task context is preserved in the chat timeline.
                continue
            run_id = str(metadata.get("run_id") or metadata.get("workflow_run_id") or "").strip()
            try:
                run = service.get_run(run_id)
            except Exception:
                logger.debug("读取 Run 失败: %s", run_id, exc_info=True)
                continue
            workflow_child_progress = None
            if metadata.get("runnable_kind") == "workflow":
                workflow_child_progress = self._sync_workflow_child_run_messages(service, run)
            status = str(run.get("status") or "").strip()
            normalized_status = self._normalize_agent_run_status(status)
            if (
                normalized_status == "approval_required"
                and metadata.get("runnable_kind") == "workflow"
                and self._workflow_waiting_for_child_approval(run)
            ):
                workflow_sender = metadata.get("sender") if isinstance(metadata.get("sender"), dict) else {}
                workflow_name = str(workflow_sender.get("nickname") or workflow_sender.get("name") or run.get("runnable_name") or "Workflow")
                waiting_context = self._workflow_child_approval_context(run, service)
                summary = (
                    f"{workflow_name} 正在等待子 Agent 审批。"
                    f"{self._workflow_child_approval_notice(waiting_context)}\n\n"
                    "处理上述子 Agent 的审批请求后，Workflow 会继续执行后续步骤。"
                )
                metadata_update = {
                    "run_id": run.get("run_id") or "",
                    "workflow_run_id": run.get("run_id") or "",
                    "run_group_id": run.get("run_group_id") or "",
                    "run_status": "processing",
                    "workflow_status": normalized_status,
                    "pending_approval": {},
                }
                if waiting_context.get("child_run_id"):
                    metadata_update["workflow_waiting_child_run_id"] = waiting_context["child_run_id"]
                if waiting_context.get("tool"):
                    metadata_update["workflow_waiting_tool"] = waiting_context["tool"]
                if waiting_context.get("workflow_node_label"):
                    metadata_update["workflow_waiting_node"] = waiting_context["workflow_node_label"]
                if isinstance(waiting_context.get("pending_approval"), dict):
                    metadata_update["workflow_waiting_pending_approval"] = waiting_context["pending_approval"]
                self._session.update_assistant_message(
                    msg.message_id,
                    summary,
                    status=MessageStatus.PROCESSING,
                    error=None,
                    metadata=metadata_update,
                )
                continue
            if normalized_status in {"processing", "pending"}:
                if str(metadata.get("run_status") or metadata.get("workflow_status") or "").strip() == "approval_required":
                    sender = metadata.get("sender") if isinstance(metadata.get("sender"), dict) else {}
                    self._update_agent_run_message_from_result(
                        msg.message_id,
                        sender,
                        run,
                        notify_group_summary=notify_group_summary,
                    )
                    continue
                sender = metadata.get("sender") if isinstance(metadata.get("sender"), dict) else {}
                if metadata.get("runnable_kind") == "workflow":
                    title, detail = workflow_child_progress or self._workflow_run_progress_from_timeline(sender, run)
                else:
                    title, detail = self._agent_run_progress_from_timeline(sender, run)
                if (
                    str(metadata.get("run_progress_title") or "") != title
                    or str(metadata.get("run_progress_detail") or "") != detail
                    or str(metadata.get("run_status") or metadata.get("workflow_status") or "") != normalized_status
                ):
                    metadata_update = {
                        "run_status": normalized_status,
                        "run_id": run.get("run_id") or "",
                        "run_group_id": run.get("run_group_id") or "",
                        "run_progress_title": title,
                        "run_progress_detail": detail,
                        "workflow_waiting_child_run_id": None,
                        "workflow_waiting_tool": None,
                        "workflow_waiting_node": None,
                        "workflow_waiting_pending_approval": None,
                    }
                    if metadata.get("runnable_kind") == "workflow" or metadata.get("workflow_status"):
                        metadata_update["workflow_status"] = normalized_status
                    self._session.update_assistant_message(
                        msg.message_id,
                        "",
                        status=MessageStatus.PROCESSING,
                        error=None,
                        metadata=metadata_update,
                    )
                continue
            if normalized_status == "":
                continue
            sender = metadata.get("sender") if isinstance(metadata.get("sender"), dict) else {}
            self._update_agent_run_message_from_result(
                msg.message_id,
                sender,
                run,
                notify_group_summary=notify_group_summary,
            )

    def _start_native_direct_group_dispatch(self, *, task: Any, original_goal: str,
            source_message_id: str, context: dict[str, Any]) -> tuple[str, str]:
        from apps.core.executor import NativeAgentExecutor

        service = self._agent_runtime_service()
        self._state.update_task_status(task.task_id, TaskStatus.RUNNING, progress_label="正在派发群组任务")
        source_text = self._format_group_dispatch_direct_source()
        assistant_id = self._session.upsert_assistant_message(task_id=task.task_id,
            content=source_text, status=MessageStatus.PROCESSING,
            metadata={"sender": self._main_model_sender_from_runtime(), "group_dispatch_direct": True})
        parent = None
        try:
            parent = service.start_main_chat_run(task_id=task.task_id, session_id=self._session.session_id,
                user_goal=original_goal, client_run_id=f"chat-group-parent:{task.task_id}",
                metadata={"source_message_id": source_message_id, "native_group_owner": "chat_api_direct"})
            selected_ids = {str(item["id"]) for item in context.get("participants") or []
                            if isinstance(item, dict) and item.get("kind") == "agent" and item.get("id")}
            plan = service.prepare_main_chat_delegation(parent["run_id"], selected_ids=selected_ids,
                    group_scope=True, direct_group=True)
            if plan is None:
                raise AgentRuntimeError("群组目标缺少可验证的委派计划")
            proposals = [{"kind": "agent", "runnable_id": target["runnable_id"], "goal": target["goal"]}
                         for target in plan["binding"]["targets"]]
            upstream = self._with_group_context_for_main_model(original_goal, context)
            upstream += "\n\n[Oha-Yachiyo 群组执行约定]\n请按当前岗位职责处理已绑定子目标。"
            children = service.start_main_chat_delegation(parent["run_id"], proposals,
                        task_id=task.task_id, group=True, upstream=upstream)
            names = [target["alias"] for target in plan["binding"]["targets"]]
            dispatch_text = f"我把这个任务派给 {names[0]} 了。" if len(names) == 1 else f"我把 {len(names)} 个任务分别派给 {'、'.join(names)} 了。"
            source_text = self._format_group_dispatch_visible_content(source_text, dispatch_text)
            NativeAgentExecutor._project_native_group_dispatch(self._session, task, plan, children, source_text)
            self._session.update_assistant_message(assistant_id, source_text, status=MessageStatus.PROCESSING,
                metadata={"run_id": parent["run_id"], "run_status": "running", "native_group_parent": True})
            return assistant_id, "processing"
        except Exception as exc:
            if parent is not None:
                from apps.shell.agent.runtime.main_chat_delegation import MainChatDelegationCoordinator

                MainChatDelegationCoordinator(service).cancel_children(parent["run_id"])
                service.fail_main_chat_run(parent["run_id"], exc)
            current = self._state.get_task(task.task_id)
            if current and current.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                self._state.update_task_status(task.task_id, TaskStatus.FAILED, error=redact_api_error_text(exc))
            self._session.update_assistant_message(assistant_id, "群组任务派发失败", status=MessageStatus.FAILED,
                error=redact_api_error_text(exc), metadata={"run_id": parent["run_id"] if parent else "", "run_status": "failed"})
            return assistant_id, "failed"

    def _sync_native_direct_group_tasks(self, *, notify_group_summary: bool) -> None:
        from types import SimpleNamespace

        from apps.shell.agent.runtime.main_chat_delegation import MainChatDelegationCoordinator, bound_plan

        service = None
        for message in self._session.get_all_messages():
            if message.role != MessageRole.ASSISTANT or not message.task_id or not (message.metadata or {}).get("native_group_parent"):
                continue
            task = self._state.get_task(message.task_id)
            durable_only = task is None
            if task is None:
                task = SimpleNamespace(task_id=message.task_id, status=TaskStatus.RUNNING)
            if task.status not in {TaskStatus.RUNNING, TaskStatus.CANCELLED}:
                continue
            try:
                service = service or self._agent_runtime_service()
                coordinator = MainChatDelegationCoordinator(service)
                parent = service.get_run(str(message.metadata.get("run_id") or ""))
                starts = [event for event in parent["timeline"] if event.get("event") == "run.started"]
                link = service.get_task_run_link(task.task_id)
                if (len(starts) != 1 or (starts[0].get("metadata") or {}).get("native_group_owner") != "chat_api_direct"
                    or link.get("run_id") != parent["run_id"] or link.get("session_id") != self._session.session_id):
                    raise AgentRuntimeError("群组父任务的持久绑定已失效")
            except Exception as exc:
                self._fail_native_direct_group_projection(message, exc)
                continue
            try:
                if task.status == TaskStatus.CANCELLED:
                    coordinator.cancel_children(parent["run_id"])
                    service.cancel_run(parent["run_id"])
                    self._cancel_native_group_summary_task(parent)
                    continue
                if parent.get("status") == "cancelled":
                    coordinator.cancel_children(parent["run_id"])
                    self._cancel_native_group_summary_task(parent)
                    if not durable_only:
                        self._state.cancel_task(task.task_id)
                    else:
                        self._session.update_assistant_message(message.message_id, message.content,
                            status=MessageStatus.FAILED, error="任务已取消", metadata={"run_status": "cancelled"})
                    continue
                plan = bound_plan(parent)
                children = coordinator.owned_children(parent)
                for child in children:
                    child_message = next((item for item in self._session.get_all_messages()
                        if (item.metadata or {}).get("run_id") == child["run_id"]), None)
                    if child_message:
                        self._update_agent_run_message_from_result(child_message.message_id,
                            child_message.metadata.get("sender") or {}, child, notify_group_summary=False)
                failed = next((child for child in children if child.get("status") in {"failed", "cancelled", "awaiting_user"}), None)
                if failed or parent.get("status") == "failed":
                    raise AgentRuntimeError(str((failed or parent).get("result") or "群组子目标未完成"))
                if any(child.get("status") != "completed" or child.get("pending_approval") for child in children):
                    continue
                if parent.get("status") == "running":
                    service.verify_main_chat_delegation(parent["run_id"])
                    if notify_group_summary:
                        self._maybe_create_group_agent_summary_task(task.task_id)
                    if plan["binding"].get("summary_required"):
                        current_message = self._session.get_assistant_message_for_task(task.task_id)
                        summary_id = str(((current_message.metadata if current_message else {}) or {}).get("group_agent_summary_task_id") or "")
                        summary = self._state.get_task(summary_id)
                        if summary is None:
                            if not summary_id and not notify_group_summary:
                                continue
                            durable_summary = coordinator.owned_summary(service.get_run(parent["run_id"]))
                            if durable_summary.get("status") != "completed" or durable_summary.get("pending_approval"):
                                raise AgentRuntimeError("群组总结任务状态已丢失，需要重新生成总结")
                        else:
                            service.bind_main_chat_delegation_summary(parent["run_id"], summary)
                            if summary.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                                raise AgentRuntimeError(summary.error or "群组总结未完成")
                            if summary.status != TaskStatus.COMPLETED:
                                continue
                        service.verify_main_chat_delegation_summary(parent["run_id"])
                    parent = coordinator.settle(parent["run_id"], message.content)
                if parent.get("status") == "completed":
                    if not durable_only:
                        self._state.update_task_status(task.task_id, TaskStatus.COMPLETED, result=str(parent.get("result") or ""))
                    self._session.update_assistant_message(message.message_id, message.content,
                        status=MessageStatus.PROCESSING if (message.metadata or {}).get("group_agent_summary_pending") else MessageStatus.COMPLETED,
                        metadata={"run_status": "completed"})
            except Exception as exc:
                try:
                    coordinator.cancel_children(parent["run_id"])
                except Exception:
                    logger.debug("群组子任务绑定已失效，无法安全取消", exc_info=True)
                try:
                    service.fail_main_chat_run(parent["run_id"], exc)
                except Exception:
                    logger.debug("群组父任务终态投影失败", exc_info=True)
                self._fail_native_direct_group_projection(message, exc)

    def _fail_native_direct_group_projection(self, message: ChatMessage, error: Exception) -> None:
        current = self._state.get_task(message.task_id)
        if current and current.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self._state.update_task_status(current.task_id, TaskStatus.FAILED, error=redact_api_error_text(error))
        self._session.update_assistant_message(message.message_id, message.content,
            status=MessageStatus.FAILED, error=redact_api_error_text(error),
            metadata={"native_group_parent": False, "run_status": "failed", "group_agent_summary_pending": False})

    def _cancel_native_group_summary_task(self, parent: dict[str, Any]) -> None:
        from apps.shell.agent.runtime.main_chat_delegation import SUMMARY_BOUND_EVENT, SOURCE, _digest, bound_plan

        plan = bound_plan(parent)
        bindings = [event for event in parent["timeline"] if event.get("event") == SUMMARY_BOUND_EVENT]
        if plan is None or len(bindings) != 1:
            return
        binding = bindings[0]
        task = self._state.get_task(str(binding.get("task_id") or ""))
        if (task is None or binding.get("source") != SOURCE or binding.get("parent_run_id") != parent["run_id"]
            or binding.get("binding_id") != plan["binding_id"] or task.description != binding.get("goal")
            or task.chat_session_id != binding.get("session_id") or _digest(task.response_context) != binding.get("context_digest")):
            return
        runner = getattr(self._runtime, "task_runner", None)
        if runner is not None:
            runner.cancel_task(task.task_id)
        if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            self._state.cancel_task(task.task_id)

    def _sync_group_dispatches_from_completed_tasks(self, *, notify_group_summary: bool = True) -> None:
        context = self._session_context()
        if context.get("conversation_kind") != "group":
            return
        for msg in self._session.get_all_messages():
            if msg.role != MessageRole.ASSISTANT or not msg.task_id:
                continue
            native_metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            if native_metadata.get("group_dispatch_handled") and notify_group_summary:
                parent_task = self._state.get_task(msg.task_id)
                if parent_task is not None and parent_task.status == TaskStatus.COMPLETED:
                    self._maybe_create_group_agent_summary_task(msg.task_id)
            if msg.status != MessageStatus.COMPLETED:
                continue
            metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            if (
                metadata.get("group_dispatch_handled")
                or metadata.get("group_dispatch_state") == "ignored"
            ):
                continue
            sender = metadata.get("sender") if isinstance(metadata.get("sender"), dict) else {}
            if sender.get("kind") in {"agent", "workflow"}:
                continue
            prepared_directives = self._prepared_group_dispatch_directives(metadata)
            task = self._state.get_task(msg.task_id)
            if task is None:
                if not (
                    prepared_directives
                    and metadata.get("group_dispatch_state") in {"prepared", "started"}
                ):
                    continue
                # A completed AppState Task is process-local.  The prepared
                # receipt is the durable authority that lets a new process
                # finish an interrupted dispatch without reinterpreting text.
                task_description = ""
                source_text = str(
                    metadata.get("group_dispatch_source_intro") or ""
                )
                directives = prepared_directives
            else:
                if task.status != TaskStatus.COMPLETED:
                    continue
                task_description = task.description
                source_text = task.result or msg.content
                directives = (
                    prepared_directives
                    if (
                        prepared_directives
                        and metadata.get("group_dispatch_state") in {"prepared", "started"}
                    )
                    else self._parse_group_dispatch_directives(source_text)
                )
            if (
                directives
                and not prepared_directives
                and not self._is_group_followup_task_description(task_description)
                and not self._group_dispatch_command_authorized(
                    task_description,
                    source_text,
                )
            ):
                self._session.update_assistant_message(
                    msg.message_id,
                    source_text,
                    status=MessageStatus.COMPLETED,
                    error=msg.error,
                    metadata={
                        "group_dispatch_state": "ignored",
                        "group_dispatch_ignored_data": True,
                        "group_dispatch_pending": None,
                        "group_dispatch_stream_visible_content": None,
                    },
                )
                continue
            if self._is_group_followup_task_description(task_description):
                cleaned_metadata = dict(metadata)
                cleaned_metadata.pop("group_dispatch_pending", None)
                cleaned_metadata.pop("group_dispatch_stream_visible_content", None)
                cleaned_metadata.update({
                    "sender": self._main_model_sender_from_runtime(),
                    "group_dispatch_handled": True,
                    "group_dispatch_count": 0,
                    "group_dispatch_skipped": ["用户补充消息不会另起群组派发"],
                    "group_followup_dispatch_ignored": True,
                })
                self._record_group_dispatch_activity(
                    task_id=msg.task_id or "",
                    title="群组补充已收下",
                    detail="补充会进入当前群组任务汇总，不会另起派发。",
                    status="completed",
                    event_id=f"{msg.task_id or msg.message_id}-group-followup-ignored-dispatch",
                )
                self._session.upsert_assistant_message(
                    task_id=msg.task_id,
                    content=self._group_followup_ack_content(),
                    status=MessageStatus.COMPLETED,
                    error=None,
                    attachments=msg.attachments,
                    metadata=cleaned_metadata,
                )
                continue
            if not directives:
                missing_expected_dispatch = self._group_dispatch_expected_without_requests(
                    task_description,
                    source_text,
                )
                fallback_directives = (
                    self._fallback_group_dispatch_directives(task_description, source_text, context)
                    if missing_expected_dispatch
                    else []
                )
                if fallback_directives:
                    fallback_source = self._format_group_dispatch_fallback_source()
                    self._dispatch_group_agent_requests(
                        msg,
                        fallback_directives,
                        context,
                        source_text=fallback_source,
                        notify_group_summary=notify_group_summary,
                    )
                    continue
                if metadata.get("group_dispatch_pending") or metadata.get("group_dispatch_stream_visible_content"):
                    cleaned_metadata = dict(metadata)
                    cleaned_metadata.pop("group_dispatch_pending", None)
                    cleaned_metadata.pop("group_dispatch_stream_visible_content", None)
                    visible_content = self._format_group_dispatch_visible_content(source_text, "")
                    if missing_expected_dispatch:
                        visible_content = self._format_group_dispatch_missing_dispatch_content(
                            visible_content or source_text
                        )
                        cleaned_metadata.update({
                            "sender": self._main_model_sender_from_runtime(),
                            "group_dispatch_handled": True,
                            "group_dispatch_count": 0,
                            "group_dispatch_skipped": [self._group_dispatch_missing_request_reason()],
                            "group_dispatch_missing_request": True,
                        })
                        self._record_group_dispatch_activity(
                            task_id=msg.task_id or "",
                            title="群组任务未派发",
                            detail=self._group_dispatch_missing_request_reason(),
                            status="failed",
                            event_id=f"{msg.task_id or msg.message_id}-group-dispatch-missing",
                        )
                    self._session.upsert_assistant_message(
                        task_id=msg.task_id,
                        content=visible_content or source_text,
                        status=MessageStatus.COMPLETED,
                        error=msg.error,
                        attachments=msg.attachments,
                        metadata=cleaned_metadata,
                    )
                elif missing_expected_dispatch:
                    visible_content = self._format_group_dispatch_missing_dispatch_content(source_text)
                    self._record_group_dispatch_activity(
                        task_id=msg.task_id or "",
                        title="群组任务未派发",
                        detail=self._group_dispatch_missing_request_reason(),
                        status="failed",
                        event_id=f"{msg.task_id or msg.message_id}-group-dispatch-missing",
                    )
                    self._session.update_assistant_message(
                        msg.message_id,
                        visible_content,
                        status=MessageStatus.COMPLETED,
                        metadata={
                            "sender": self._main_model_sender_from_runtime(),
                            "group_dispatch_handled": True,
                            "group_dispatch_count": 0,
                            "group_dispatch_skipped": [self._group_dispatch_missing_request_reason()],
                            "group_dispatch_missing_request": True,
                        },
                    )
                continue
            fallback_directives = self._missing_group_dispatch_fallback_directives(
                directives,
                self._fallback_group_dispatch_directives(task_description, source_text, context),
                context,
            )
            if fallback_directives:
                directives = [*directives, *fallback_directives][:3]
                source_text = self._format_group_dispatch_partial_fallback_source(source_text)
            self._dispatch_group_agent_requests(
                msg,
                directives,
                context,
                source_text=source_text,
                notify_group_summary=notify_group_summary,
            )

    @classmethod
    def _parse_group_dispatch_directives(cls, content: str) -> list[GroupDispatchDirective]:
        directives: list[GroupDispatchDirective] = []
        for payload in cls._json_payloads_from_text(content):
            directives.extend(cls._group_dispatch_directives_from_payload(payload))
        deduped: list[GroupDispatchDirective] = []
        seen: set[tuple[str, str, str]] = set()
        for item in directives:
            key = (item.kind, item.target, item.goal)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:3]

    @classmethod
    def _parse_group_dispatch_requests(cls, content: str) -> list[dict[str, str]]:
        return [directive.as_request() for directive in cls._parse_group_dispatch_directives(content)]

    @staticmethod
    def _prepared_group_dispatch_directives(
        metadata: dict[str, Any],
    ) -> list[GroupDispatchDirective]:
        payload = metadata.get("group_dispatch_prepared")
        if not isinstance(payload, list):
            return []
        directives: list[GroupDispatchDirective] = []
        for item in payload[:3]:
            if not isinstance(item, dict):
                continue
            directive = GroupDispatchDirective(
                kind=str(item.get("kind") or ""),
                target=str(item.get("target") or ""),
                runnable_id=str(item.get("runnable_id") or ""),
                goal=str(item.get("goal") or ""),
            )
            if (
                directive.kind == "agent"
                and directive.runnable_id
                and directive.goal
            ):
                directives.append(directive)
        return directives

    @classmethod
    def _group_dispatch_command_authorized(
        cls,
        task_description: str,
        response_text: str,
    ) -> bool:
        """Require both user execution intent and executable payload provenance.

        JSON is also a common data/documentation format.  A syntactically valid
        dispatch-shaped object is therefore not authority by itself: the user
        request must authorize real dispatch and the model output must present
        the object as a command rather than a quotation.
        """

        request = cls._group_dispatch_user_request_from_task(task_description)
        if not request:
            return False
        if not cls._group_dispatch_request_authorizes_execution(
            task_description,
            response_text,
        ):
            return False
        return cls._group_dispatch_payload_has_command_provenance(response_text)

    @classmethod
    def _group_dispatch_request_authorizes_execution(
        cls,
        task_description: str,
        response_text: str,
    ) -> bool:
        request = cls._group_dispatch_user_request_from_task(task_description)
        instruction = cls._text_outside_quoted_data(request)
        compact = re.sub(r"\s+", "", instruction)
        if not compact:
            return False

        # An explicit prohibition always wins over dispatch-looking data that
        # follows it.  This is capability authority, not a model suggestion.
        if re.search(
            r"(?:不要|不用|不需要|无需|先不|禁止|仅供|只供|do\s+not|don't|dont|must\s+not|without)"
            r".{0,24}(?:执行|运行|派|派发|委派|分配|指派|交给|dispatch|delegate|assign|execute|run)",
            compact,
            re.IGNORECASE,
        ):
            return False

        force_execution = bool(
            re.search(
                r"(?:实际|真正|立即|现在|开始)"
                r".{0,20}(?:执行|运行|派|派发|委派|分配|指派|交给|dispatch|delegate|assign|execute|run)",
                instruction,
                re.IGNORECASE,
            )
            or re.search(
                r"(?:请|帮我|麻烦|please).{0,30}"
                r"(?:执行|运行|派给|交给|分配给|指派给|委派给|execute|run|dispatch.{0,12}\bto\b|assign.{0,12}\bto\b|delegate.{0,12}\bto\b)",
                instruction,
                re.IGNORECASE,
            )
        )
        data_artifact = bool(re.search(
            r"(?:json|payload|schema|代码块|指令文本|命令文本|样例|示例|例子|模板|格式|数据|引用|"
            r"机制|协议|字段|内容|文本|模型输出|请求体|安全性|"
            r"sample|example|template|format|data|quote|quoted|documentation|mechanism|protocol|output|request\s+body)",
            instruction,
            re.IGNORECASE,
        ))
        data_operation = bool(re.search(
            r"(?:审计|分析|检查|评审|解释|说明|展示|输出|复制|引用|改写|翻译|"
            r"audit|analy[sz]e|inspect|review|explain|describe|show|print|copy|quote|rewrite|translate)",
            instruction,
            re.IGNORECASE,
        ))
        if data_artifact and data_operation and not force_execution:
            return False

        if cls._group_dispatch_expected_without_requests(
            task_description,
            response_text,
        ):
            return True
        return bool(re.search(
            r"(?:派|派发|派活|委派|安排|分配|指派|交给|让|分工|协作|一起|跑|执行|运行|开始|做|处理|"
            r"dispatch|delegate|assign|hand\s+off|collaborate|run|execute|start|do)",
            instruction,
            re.IGNORECASE,
        ))

    @staticmethod
    def _text_outside_quoted_data(text: str) -> str:
        """Drop quoted payload bodies while preserving surrounding instructions."""

        lines: list[str] = []
        in_fence = False
        for line in str(text or "").splitlines():
            if re.match(r"^\s*```", line):
                in_fence = not in_fence
                continue
            if in_fence or re.match(r"^\s*>", line):
                continue
            lines.append(line)
        instruction = "\n".join(lines)
        instruction = re.sub(r"`[^`]*`", " ", instruction, flags=re.DOTALL)
        instruction = re.sub(r"[「『“‘].*?[」』”’]", " ", instruction, flags=re.DOTALL)
        # A JSON body may be pasted directly after an instruction.  Its keys
        # and values cannot grant authority to execute the instruction.
        json_body = re.search(
            r"[\[{]\s*(?:[\[{\"'\d\]-]|true\b|false\b|null\b)",
            instruction,
            re.IGNORECASE,
        )
        if json_body is not None:
            instruction = instruction[: json_body.start()]
        return instruction.strip()

    @classmethod
    def _group_dispatch_payload_has_command_provenance(cls, content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return False

        outside_quotes: list[str] = []
        fenced_blocks: list[str] = []
        current_fence: list[str] = []
        in_fence = False
        for line in text.splitlines():
            if re.match(r"^\s*```", line):
                if in_fence:
                    fenced_blocks.append("\n".join(current_fence))
                    current_fence = []
                in_fence = not in_fence
                continue
            if in_fence:
                current_fence.append(line)
                continue
            if re.match(r"^\s*>", line):
                continue
            outside_quotes.append(line)
        outside_text = re.sub(
            r"[「『“‘].*?[」』”’]",
            " ",
            "\n".join(outside_quotes),
            flags=re.DOTALL,
        )
        outside_text = re.sub(r"`[^`]*`", " ", outside_text, flags=re.DOTALL)
        if re.search(
            r"<\s*(?:oha|native)[\s_-]*group[\s_-]*dispatch\b[^>]*>.*?"
            r"</\s*(?:oha|native)[\s_-]*group[\s_-]*dispatch\s*>",
            outside_text,
            re.DOTALL | re.IGNORECASE,
        ):
            return True
        if cls._parse_group_dispatch_directives(outside_text):
            return True

        # A whole-document JSON fence is accepted as a deliberate structured
        # command, but an embedded documentation/example fence is not.
        if fenced_blocks and not "\n".join(outside_quotes).strip():
            return any(cls._parse_group_dispatch_directives(block) for block in fenced_blocks)
        return False

    @staticmethod
    def _coerce_group_dispatch_directive(request: Any) -> GroupDispatchDirective:
        if isinstance(request, GroupDispatchDirective):
            return request
        if isinstance(request, dict):
            return GroupDispatchDirective(
                kind=str(request.get("kind") or request.get("action") or "agent"),
                target=str(
                    request.get("target")
                    or request.get("agent")
                    or request.get("name")
                    or request.get("nickname")
                    or ""
                ),
                runnable_id=str(request.get("runnable_id") or request.get("runnableId") or request.get("id") or ""),
                goal=str(request.get("goal") or request.get("user_goal") or request.get("task") or ""),
            )
        return GroupDispatchDirective(kind="", goal="")

    @staticmethod
    def _group_dispatch_missing_request_reason() -> str:
        return "主模型没有生成可执行的群组 Agent 派发请求"

    @classmethod
    def _fallback_group_dispatch_directives(
        cls,
        task_description: str,
        response_text: str,
        context: dict[str, Any],
    ) -> list[GroupDispatchDirective]:
        request = cls._group_dispatch_user_request_from_task(task_description)
        if not request:
            return []
        if cls._group_dispatch_response_declines_dispatch(response_text):
            return []
        participants = [
            item
            for item in (context.get("participants") or [])
            if isinstance(item, dict) and item.get("kind") == "agent"
        ]
        if not participants:
            return []
        target_text = f"{request}\n{response_text}"
        matched = [
            participant
            for participant in participants
            if cls._fallback_group_dispatch_participant_mentioned(participant, target_text)
        ]
        directives: list[GroupDispatchDirective] = []
        seen: set[str] = set()
        for participant in matched[:3]:
            participant_id = str(participant.get("id") or "").strip()
            if not participant_id or participant_id in seen:
                continue
            seen.add(participant_id)
            display_name = str(
                participant.get("nickname")
                or participant.get("name")
                or participant_id
            ).strip()
            directives.append(GroupDispatchDirective(
                kind="agent",
                target=display_name,
                runnable_id=participant_id,
                goal=cls._fallback_group_dispatch_goal(request, participant),
            ))
        return directives

    @classmethod
    def _fallback_group_dispatch_requests(
        cls,
        task_description: str,
        response_text: str,
        context: dict[str, Any],
    ) -> list[dict[str, str]]:
        return [
            directive.as_request()
            for directive in cls._fallback_group_dispatch_directives(task_description, response_text, context)
        ]

    @classmethod
    def _direct_group_dispatch_directives(
        cls,
        user_text: str,
        context: dict[str, Any],
    ) -> list[GroupDispatchDirective]:
        request = str(user_text or "").strip()
        if cls._parse_main_model_mention(request) is not None:
            return []
        if not request or not cls._group_user_text_has_direct_dispatch_intent(request):
            return []
        participants = [
            item
            for item in (context.get("participants") or [])
            if isinstance(item, dict) and item.get("kind") == "agent"
        ]
        if not participants:
            return []
        matched = [
            participant
            for participant in participants
            if cls._fallback_group_dispatch_participant_mentioned(participant, request)
        ]
        directives: list[GroupDispatchDirective] = []
        seen: set[str] = set()
        for participant in matched[:3]:
            participant_id = str(participant.get("id") or "").strip()
            if not participant_id or participant_id in seen:
                continue
            seen.add(participant_id)
            display_name = str(
                participant.get("nickname")
                or participant.get("name")
                or participant_id
            ).strip()
            directives.append(GroupDispatchDirective(
                kind="agent",
                target=display_name,
                runnable_id=participant_id,
                goal=cls._direct_group_dispatch_goal(request, participant),
            ))
        return directives

    @classmethod
    def _direct_group_dispatch_requests(
        cls,
        user_text: str,
        context: dict[str, Any],
    ) -> list[dict[str, str]]:
        return [
            directive.as_request()
            for directive in cls._direct_group_dispatch_directives(user_text, context)
        ]

    @staticmethod
    def _group_user_text_has_direct_dispatch_intent(user_text: str) -> bool:
        text = str(user_text or "").strip()
        if not text:
            return False
        compact = re.sub(r"\s+", "", text)
        if re.search(r"(?:不要|不用|不需要|无需|先不).{0,16}(?:派|派发|派活|安排|分配|指派|交给|让|Agent|agent)", compact, re.IGNORECASE):
            return False
        directive = re.search(
            r"(请|帮我?|麻烦|劳烦|让|安排|派发?|派活|委派|分配|指派|交给|分别|一起|协作|配合|"
            r"please|ask|have|let|assign|dispatch|delegate)",
            text,
            re.IGNORECASE,
        )
        if not directive:
            return False
        action = re.search(
            r"(做|处理|产出|输出|实现|编写|写|运行|验证|整理|设计|开发|修复|修改|审批|"
            r"build|create|implement|write|run|review|design|code|fix|verify)",
            text,
            re.IGNORECASE,
        )
        collaboration = re.search(r"(一起|分别|协作|配合|并行|同时|together|parallel)", text, re.IGNORECASE)
        return bool(action or collaboration)

    @classmethod
    def _direct_group_dispatch_goal(cls, request: str, participant: dict[str, Any]) -> str:
        display_name = str(
            participant.get("nickname")
            or participant.get("name")
            or participant.get("id")
            or "Agent"
        ).strip() or "Agent"
        details = []
        category = str(participant.get("category") or "").strip()
        if category:
            details.append(f"类别：{category}")
        description = str(participant.get("description") or "").strip()
        if description:
            details.append(f"职责：{description}")
        detail_text = f"（{'；'.join(details)}）" if details else ""
        return (
            "这是群组用户消息的直接派发：用户明确点名你参与执行。"
            "请实际完成你负责的部分，不要只复述安排。\n\n"
            f"你的身份：{display_name}{detail_text}\n\n"
            f"用户原始目标：\n{request}"
        )

    @staticmethod
    def _format_group_dispatch_direct_source() -> str:
        return "用户已明确点名群内 Agent；我会直接派发真实任务，等待 Agent 完成后再汇总。"

    @classmethod
    def _missing_group_dispatch_fallback_directives(
        cls,
        directives: list[GroupDispatchDirective],
        fallback_directives: list[GroupDispatchDirective],
        context: dict[str, Any],
    ) -> list[GroupDispatchDirective]:
        if not directives or not fallback_directives:
            return []
        participants = [
            item
            for item in (context.get("participants") or [])
            if isinstance(item, dict) and item.get("kind") == "agent"
        ]
        dispatched_ids: set[str] = set()
        for directive in directives:
            runnable_id = directive.runnable_id
            if runnable_id:
                dispatched_ids.add(runnable_id)
            target_text = "\n".join(
                (directive.target, directive.runnable_id)
            )
            for participant in participants:
                participant_id = str(participant.get("id") or "").strip()
                if participant_id and cls._fallback_group_dispatch_participant_mentioned(participant, target_text):
                    dispatched_ids.add(participant_id)
        missing: list[GroupDispatchDirective] = []
        seen: set[str] = set(dispatched_ids)
        for directive in fallback_directives:
            runnable_id = directive.runnable_id
            if not runnable_id or runnable_id in seen:
                continue
            seen.add(runnable_id)
            missing.append(directive)
        return missing

    @classmethod
    def _missing_group_dispatch_fallback_requests(
        cls,
        requests: list[dict[str, str]],
        fallback_requests: list[dict[str, str]],
        context: dict[str, Any],
    ) -> list[dict[str, str]]:
        directives = [cls._coerce_group_dispatch_directive(request) for request in requests]
        fallback_directives = [cls._coerce_group_dispatch_directive(request) for request in fallback_requests]
        return [
            directive.as_request()
            for directive in cls._missing_group_dispatch_fallback_directives(directives, fallback_directives, context)
        ]

    @classmethod
    def _fallback_group_dispatch_participant_mentioned(
        cls,
        participant: dict[str, Any],
        text: str,
    ) -> bool:
        normalized_text = cls._normalize_group_agent_alias(text)
        if not normalized_text:
            return False
        compact_text = normalized_text.replace(" ", "")
        aliases: set[str] = set()
        for key in ("id", "name", "nickname"):
            normalized = cls._normalize_group_agent_alias(participant.get(key))
            if normalized:
                aliases.add(normalized)
            aliases.update(cls._group_agent_alias_tokens(participant.get(key)))
        aliases.discard("")
        generic = {"agent", "assistant", "main", "group", "member", "成员", "群成员", "主模型"}
        aliases -= generic
        for alias in aliases:
            if alias in normalized_text:
                return True
            compact_alias = alias.replace(" ", "")
            if len(compact_alias) >= 3 and compact_alias in compact_text:
                return True
        return False

    @classmethod
    def _fallback_group_dispatch_goal(cls, request: str, participant: dict[str, Any]) -> str:
        display_name = str(
            participant.get("nickname")
            or participant.get("name")
            or participant.get("id")
            or "Agent"
        ).strip() or "Agent"
        details = []
        category = str(participant.get("category") or "").strip()
        if category:
            details.append(f"类别：{category}")
        description = str(participant.get("description") or "").strip()
        if description:
            details.append(f"职责：{description}")
        detail_text = f"（{'；'.join(details)}）" if details else ""
        return (
            "这是群组自然目标的兜底派发：主模型说明了分工但没有生成机器派发请求。"
            "请实际执行你负责的部分，不要沿用主模型声称已经完成的结果。\n\n"
            f"你的身份：{display_name}{detail_text}\n\n"
            f"用户原始目标：\n{request}"
        )

    @staticmethod
    def _format_group_dispatch_fallback_source() -> str:
        return (
            "主模型没有生成可执行的群组派发请求；"
            "我已根据用户明确提到的群内 Agent 创建真实任务，等待 Agent 完成后再汇总。"
        )

    @classmethod
    def _format_group_dispatch_partial_fallback_source(cls, source_text: str) -> str:
        visible = cls._normalize_group_dispatch_intro(cls._strip_group_dispatch_payloads(source_text)).strip()
        notice = "主模型只生成了部分群组派发请求；我已根据用户明确提到的群内 Agent 补齐遗漏任务。"
        if not visible:
            return notice
        if notice in visible:
            return visible
        return f"{visible}\n\n{notice}"

    @classmethod
    def _group_dispatch_expected_without_requests(cls, task_description: str, response_text: str) -> bool:
        if cls._group_dispatch_response_declines_dispatch(response_text):
            return False
        request = cls._group_dispatch_user_request_from_task(task_description)
        if not request:
            return False
        compact = re.sub(r"\s+", "", request, flags=re.IGNORECASE)
        if re.search(r"(?:不要|不用|不需要|无需|先不).{0,12}(?:派|派发|派活|安排|分配|指派|交给|agent)", compact, re.IGNORECASE):
            return False
        if re.search(r"(派发|派活|委派|dispatch)", request, re.IGNORECASE):
            return True
        response = str(response_text or "")
        target_text = f"{request}\n{response}"
        participant_names = cls._group_dispatch_agent_names_from_task(task_description)
        target_cue = bool(re.search(
            r"(Agent|agent|代理|群成员|群内|群里|群组|其他.{0,12}Agent|多个.{0,12}Agent|"
            r"多.{0,8}Agent|协作|团队)",
            target_text,
            re.IGNORECASE,
        ))
        if not target_cue:
            target_cue = any(name and name in target_text for name in participant_names)
        if not target_cue:
            return False
        return bool(re.search(r"(安排|分配|指派|交给|给.{0,24}|让.{0,24})", request, re.IGNORECASE))

    @staticmethod
    def _group_dispatch_user_request_from_task(task_description: str) -> str:
        text = str(task_description or "")
        return _text_before_first_marker(text, _GROUP_CONTEXT_MARKERS).strip()

    @staticmethod
    def _group_dispatch_agent_names_from_task(task_description: str) -> list[str]:
        text = str(task_description or "")
        if not _has_any_marker(text, _GROUP_CONTEXT_MARKERS):
            return []
        names: list[str] = []
        for line in text.splitlines():
            if "（Agent" not in line and "(Agent" not in line:
                continue
            match = re.match(r"\s*-\s*([^（(]+)", line)
            if not match:
                continue
            name = match.group(1).strip()
            if name:
                names.append(name)
        return names

    @staticmethod
    def _group_dispatch_response_declines_dispatch(response_text: str) -> bool:
        text = str(response_text or "")
        compact = re.sub(r"\s+", "", text, flags=re.IGNORECASE)
        if re.search(r"(?:不需要|不用|无需|先不).{0,16}(?:派|派发|派活|安排|分配|交给|其他Agent|Agent)", compact, re.IGNORECASE):
            return True
        if re.search(r"(?:我可以|我先|我来)?直接回答", compact):
            return True
        return False

    @classmethod
    def _format_group_dispatch_missing_dispatch_content(cls, source_text: str) -> str:
        content = cls._normalize_group_dispatch_intro(cls._strip_group_dispatch_payloads(source_text)).strip()
        notice = (
            "这次没有实际派出 Agent：主模型没有生成可执行的群组 Agent 派发请求。"
            "你可以重新说明要交给哪个 Agent，或直接 @ 群内 Agent。"
        )
        if not content:
            return notice
        if notice in content:
            return content
        return f"{content}\n\n{notice}"

    def _should_hide_group_dispatch_stream(
        self,
        task_description: str,
        content: str,
        context: dict[str, Any],
    ) -> bool:
        if context.get("conversation_kind") != "group":
            return False
        if not _has_any_marker(str(task_description or ""), _GROUP_CONTEXT_MARKERS):
            return False
        text = (content or "").strip()
        if not text:
            return False
        if self._parse_group_dispatch_directives(text):
            return True
        lowered = text.lower()
        compact = re.sub(r"[\s_-]+", "", lowered)
        if re.search(r"<\s*(?:oha|native)[\s_-]*group[\s_-]*dispatch\b", text, re.IGNORECASE):
            return True
        if "ohagroupdispatch" in compact or "nativegroupdispatch" in compact:
            return True
        if "dispatchgroupagent" in compact or "runohaagent" in compact:
            return True
        if re.search(
            r"(^|\n)\s*[\[{]\s*(?:\"(?:action|tasks|agents|dispatches?|tool|agent|goal)\"|$)",
            text,
            re.DOTALL,
        ):
            return True
        if re.search(r"(^|\n)\s*```(?:json)?\s*$", text, re.IGNORECASE):
            return True
        return False

    @classmethod
    def _group_dispatch_stream_visible_content(cls, content: str, metadata: dict[str, Any]) -> str:
        visible = cls._strip_group_dispatch_payloads(content)
        visible = cls._normalize_group_dispatch_intro(visible)
        previous = str(metadata.get("group_dispatch_stream_visible_content") or "").strip()
        if previous:
            previous = cls._normalize_group_dispatch_intro(cls._strip_group_dispatch_payloads(previous))
        if visible:
            if previous and not visible.startswith(previous):
                return previous
            return visible
        if previous:
            return previous
        return ""

    @classmethod
    def _json_payloads_from_text(cls, content: str) -> list[Any]:
        text = (content or "").strip()
        if not text:
            return []
        unsupported_spans = cls._unsupported_group_dispatch_tag_spans(text)
        for start, end in reversed(cls._merge_spans(unsupported_spans)):
            text = text[:start] + text[end:]
        text = text.strip()
        if not text:
            return []
        tag_matches = list(re.finditer(
            r"<\s*(?:oha|native)[\s_-]*group[\s_-]*dispatch\b[^>]*>\s*(.*?)\s*</\s*(?:oha|native)[\s_-]*group[\s_-]*dispatch\s*>",
            text,
            re.DOTALL | re.IGNORECASE,
        ))
        if tag_matches:
            payloads: list[Any] = []
            for match in tag_matches:
                payloads.extend(cls._json_payloads_from_text(match.group(1)))
            return payloads
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()

        payloads: list[Any] = []
        for candidate in cls._json_candidate_texts(text):
            try:
                return [json.loads(candidate)]
            except (TypeError, json.JSONDecodeError):
                pass

            decoder = json.JSONDecoder()
            index = 0
            while index < len(candidate):
                char = candidate[index]
                if char not in "{[":
                    index += 1
                    continue
                try:
                    payload, offset = decoder.raw_decode(candidate[index:])
                except json.JSONDecodeError:
                    index += 1
                    continue
                payloads.append(payload)
                index += max(offset, 1)
            if payloads:
                return payloads
        return payloads

    @staticmethod
    def _json_candidate_texts(text: str) -> list[str]:
        candidates = [text]
        normalized = (
            text.replace("“", '"')
            .replace("”", '"')
            .replace("„", '"')
            .replace("＂", '"')
        )
        if normalized != text:
            candidates.append(normalized)
        return candidates

    @staticmethod
    def _payload_value(payload: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
        normalized_keys = {
            re.sub(r"[\s_-]+", "", str(key or "")).lower(): key
            for key in keys
            if str(key or "").strip()
        }
        if not normalized_keys:
            return None
        for raw_key, value in payload.items():
            if value in (None, ""):
                continue
            normalized = re.sub(r"[\s_-]+", "", str(raw_key or "")).lower()
            if normalized in normalized_keys:
                return value
        return None

    @classmethod
    def _group_dispatch_directives_from_payload(cls, payload: Any) -> list[GroupDispatchDirective]:
        if isinstance(payload, list):
            result: list[GroupDispatchDirective] = []
            for item in payload:
                result.extend(cls._group_dispatch_directives_from_payload(item))
            return result
        if not isinstance(payload, dict):
            return []
        envelope_keys = {
            "input",
            "args",
            "arguments",
            "parameters",
            "params",
            "payload",
            "request",
        }
        enveloped = cls._payload_value(
            payload,
            "input",
            "args",
            "arguments",
            "parameters",
            "params",
            "payload",
            "request",
        )
        if isinstance(enveloped, str):
            try:
                enveloped = json.loads(enveloped)
            except (TypeError, json.JSONDecodeError):
                enveloped = None
        if isinstance(enveloped, (dict, list)):
            if isinstance(enveloped, dict):
                merged = {**payload, **enveloped}
                for key in list(merged):
                    if re.sub(r"[\s_-]+", "", str(key or "")).lower() in envelope_keys:
                        merged.pop(key, None)
                return cls._group_dispatch_directives_from_payload(merged)
            result: list[GroupDispatchDirective] = []
            for item in enveloped:
                if isinstance(item, dict):
                    merged = {**payload, **item}
                    for key in list(merged):
                        if re.sub(r"[\s_-]+", "", str(key or "")).lower() in envelope_keys:
                            merged.pop(key, None)
                    result.extend(cls._group_dispatch_directives_from_payload(merged))
            return result
        nested = cls._payload_value(payload, "tasks", "dispatches", "delegations")
        if isinstance(nested, list):
            result: list[GroupDispatchDirective] = []
            for item in nested:
                if isinstance(item, dict):
                    merged = {**payload, **item}
                    for key in list(merged):
                        if re.sub(r"[\s_-]+", "", str(key or "")).lower() in {
                            "tasks",
                            "agents",
                            "dispatches",
                            "delegations",
                        }:
                            merged.pop(key, None)
                    result.extend(cls._group_dispatch_directives_from_payload(merged))
            return result
        mapped = cls._payload_value(payload, "assignments", "assignment", "tasks", "dispatches", "delegations")
        if isinstance(mapped, dict):
            result: list[GroupDispatchDirective] = []
            for target, item in mapped.items():
                if isinstance(item, dict):
                    merged = {
                        **payload,
                        **item,
                        "agent": item.get("agent") or item.get("target") or target,
                    }
                else:
                    merged = {**payload, "agent": target, "goal": item}
                cls._ensure_group_dispatch_agent_action(merged)
                for key in list(merged):
                    if re.sub(r"[\s_-]+", "", str(key or "")).lower() in {
                        "assignments",
                        "assignment",
                        "tasks",
                        "dispatches",
                        "delegations",
                    }:
                        merged.pop(key, None)
                result.extend(cls._group_dispatch_directives_from_payload(merged))
            return result
        agent_entries = cls._payload_value(payload, "agents")
        if isinstance(agent_entries, list) and any(isinstance(item, dict) for item in agent_entries):
            result: list[GroupDispatchDirective] = []
            for item in agent_entries:
                if isinstance(item, dict):
                    merged = {**payload, **item}
                    for key in list(merged):
                        if re.sub(r"[\s_-]+", "", str(key or "")).lower() == "agents":
                            merged.pop(key, None)
                    result.extend(cls._group_dispatch_directives_from_payload(merged))
            return result
        if isinstance(agent_entries, dict):
            result: list[GroupDispatchDirective] = []
            for target, item in agent_entries.items():
                if isinstance(item, dict):
                    merged = {
                        **payload,
                        **item,
                        "agent": item.get("agent") or item.get("target") or target,
                    }
                else:
                    merged = {**payload, "agent": target, "goal": item}
                cls._ensure_group_dispatch_agent_action(merged)
                for key in list(merged):
                    if re.sub(r"[\s_-]+", "", str(key or "")).lower() == "agents":
                        merged.pop(key, None)
                result.extend(cls._group_dispatch_directives_from_payload(merged))
            return result

        action = cls._group_dispatch_action_from_payload(payload)
        if action not in {"agent", "workflow"}:
            return []
        goal_values = cls._group_dispatch_goal_values(
            cls._payload_value(
                payload,
                "goal",
                "goals",
                "user_goal",
                "userGoal",
                "user_goals",
                "userGoals",
                "task",
                "tasks",
                "task_goal",
                "taskGoal",
                "task_goals",
                "taskGoals",
                "objective",
                "objectives",
                "instruction",
                "instructions",
                "prompt",
                "prompts",
            )
        )
        if not goal_values:
            return []
        if action == "agent":
            target_values = cls._group_dispatch_target_values(
                cls._payload_value(
                    payload,
                    "agent",
                    "agents",
                    "name",
                    "agent_name",
                    "agentName",
                    "assignee",
                    "target",
                    "target_name",
                    "targetName",
                    "runnable",
                    "runnable_name",
                    "runnableName",
                )
            )
            target_id_values = cls._group_dispatch_target_values(
                cls._payload_value(payload, "agent_id", "agentId", "runnable_id", "runnableId", "id")
            )
        else:
            target_values = cls._group_dispatch_target_values(
                cls._payload_value(
                    payload,
                    "workflow",
                    "workflows",
                    "name",
                    "workflow_name",
                    "workflowName",
                    "target",
                    "target_name",
                    "targetName",
                    "runnable",
                    "runnable_name",
                    "runnableName",
                )
            )
            target_id_values = cls._group_dispatch_target_values(
                cls._payload_value(payload, "workflow_id", "workflowId", "runnable_id", "runnableId", "id")
            )
        if not target_values and not target_id_values:
            return []
        count = max(len(target_values), len(target_id_values), len(goal_values), 1)
        directives: list[GroupDispatchDirective] = []
        for index in range(count):
            target = target_values[index] if index < len(target_values) else ""
            target_id = target_id_values[index] if index < len(target_id_values) else ""
            goal = goal_values[index] if index < len(goal_values) else goal_values[0]
            if not target and not target_id:
                continue
            directives.append(GroupDispatchDirective(kind=action, target=target, runnable_id=target_id, goal=goal))
        return directives

    @classmethod
    def _group_dispatch_requests_from_payload(cls, payload: Any) -> list[dict[str, str]]:
        return [directive.as_request() for directive in cls._group_dispatch_directives_from_payload(payload)]

    @staticmethod
    def _is_group_dispatch_envelope_action(action: Any) -> bool:
        compact = re.sub(r"[\s_\-./]+", "", str(action or "").strip().lower())
        return compact in {
            "groupdispatch",
            "groupagentdispatch",
            "dispatchgroup",
            "dispatchgroupagents",
            "ohagroupdispatch",
            "ohagroupagentdispatch",
            "ohadispatchgroup",
            "nativegroupdispatch",
            "nativegroupagentdispatch",
            "nativedispatchgroup",
        }

    @classmethod
    def _group_dispatch_action_from_payload(cls, payload: dict[str, Any]) -> str:
        primary = cls._payload_value(payload, "action", "kind", "type", "target_kind", "runnable_kind")
        action = cls._normalize_group_dispatch_action(str(primary or ""))
        if action:
            return action
        if cls._is_group_dispatch_envelope_action(primary):
            secondary = cls._payload_value(payload, "kind", "type", "target_kind", "runnable_kind")
            return cls._normalize_group_dispatch_action(str(secondary or "")) or "agent"
        tool = cls._payload_value(payload, "tool")
        action = cls._normalize_group_dispatch_action(str(tool or ""))
        if action:
            return action
        if cls._is_group_dispatch_envelope_action(tool):
            secondary = cls._payload_value(payload, "kind", "type", "target_kind", "runnable_kind")
            return cls._normalize_group_dispatch_action(str(secondary or "")) or "agent"
        return ""

    @staticmethod
    def _normalize_group_dispatch_action(action: str) -> str:
        compact = re.sub(r"[\s_\-./]+", "", (action or "").strip().lower())
        if compact in {
            "agent",
            "agents",
            "groupagent",
            "runagent",
            "agentrun",
            "createagentrun",
            "delegateagent",
            "delegatetoagent",
            "assignagent",
            "dispatchagent",
            "dispatchgroupagent",
            "rungroupagent",
            "runohaagent",
            "ohaagent",
            "runnativeagent",
            "nativeagent",
        }:
            return "agent"
        if compact in {
            "workflow",
            "workflows",
            "groupworkflow",
            "runworkflow",
            "workflowrun",
            "createworkflowrun",
            "delegateworkflow",
            "delegatetoworkflow",
            "assignworkflow",
            "dispatchworkflow",
            "dispatchgroupworkflow",
            "rungroupworkflow",
            "runohaworkflow",
            "ohaworkflow",
            "runnativeworkflow",
            "nativeworkflow",
        }:
            return "workflow"
        return ""

    @staticmethod
    def _clean_group_dispatch_target(value: str) -> str:
        text = " ".join(str(value or "").split()).strip()
        text = text.strip("\"'“”‘’")
        if text.startswith("@"):
            text = text[1:].strip()
        return text.strip("\"'“”‘’")

    @classmethod
    def _ensure_group_dispatch_agent_action(cls, payload: dict[str, Any]) -> None:
        if cls._payload_value(payload, "action", "tool", "kind", "type", "target_kind", "runnable_kind"):
            return
        payload["action"] = "dispatch_group_agent"

    @classmethod
    def _group_dispatch_target_values(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            targets: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    targets.extend(cls._group_dispatch_target_values(
                        cls._payload_value(
                            item,
                            "agent",
                            "workflow",
                            "name",
                            "nickname",
                            "target",
                            "runnable",
                            "id",
                        )
                    ))
                else:
                    targets.extend(cls._group_dispatch_target_values(item))
            return cls._dedupe_group_dispatch_targets(targets)
        text = cls._clean_group_dispatch_target(str(value))
        if not text:
            return []
        pieces = [
            cls._clean_group_dispatch_target(piece)
            for piece in re.split(r"[、,，;；/]+", text)
        ]
        cleaned = [piece for piece in pieces if piece]
        return cls._dedupe_group_dispatch_targets(cleaned or [text])

    @classmethod
    def _group_dispatch_goal_values(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            goals: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    goals.extend(cls._group_dispatch_goal_values(
                        cls._payload_value(
                            item,
                            "goal",
                            "user_goal",
                            "userGoal",
                            "task",
                            "task_goal",
                            "taskGoal",
                            "objective",
                            "instruction",
                            "instructions",
                            "prompt",
                        )
                    ))
                else:
                    goals.extend(cls._group_dispatch_goal_values(item))
            return goals
        text = " ".join(str(value or "").split()).strip()
        return [text] if text else []

    @staticmethod
    def _dedupe_group_dispatch_targets(targets: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for target in targets:
            key = target.lower()
            if not target or key in seen:
                continue
            seen.add(key)
            deduped.append(target)
        return deduped

    @staticmethod
    def _normalize_group_agent_alias(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = text.lstrip("@")
        text = re.sub(r"[`\"'“”‘’<>《》()（）\[\]【】{}]", " ", text)
        text = re.sub(r"[/\\|,，、:：;；._-]+", " ", text)
        return " ".join(text.split())

    @classmethod
    def _group_agent_alias_tokens(cls, value: Any) -> set[str]:
        normalized = cls._normalize_group_agent_alias(value)
        if not normalized:
            return set()
        generic = {"agent", "assistant", "main", "group", "member", "成员", "群成员", "主模型"}
        return {item for item in normalized.split() if len(item) > 1 and item not in generic}

    @classmethod
    def _group_agent_participant_aliases(cls, participant: dict[str, Any]) -> set[str]:
        aliases: set[str] = set()
        for key in ("id", "name", "nickname", "category"):
            normalized = cls._normalize_group_agent_alias(participant.get(key))
            if normalized:
                aliases.add(normalized)
            aliases.update(cls._group_agent_alias_tokens(participant.get(key)))
        category = cls._normalize_group_agent_alias(participant.get("category"))
        category_aliases = {
            "coding": {"coding", "coding agent", "code", "coder", "dev", "developer", "编码", "代码", "开发"},
            "design": {"design", "design agent", "designer", "ui", "ux", "设计"},
            "research": {"research", "research agent", "researcher", "调研", "研究"},
            "review": {"review", "review agent", "reviewer", "审查", "评审", "复核"},
            "office": {"office", "office agent", "文档", "办公"},
            "orchestrator": {"orchestrator", "主控", "协调", "调度"},
        }
        aliases.update(category_aliases.get(category, set()))
        aliases.discard("")
        return aliases

    @classmethod
    def _group_agent_participant_matches_target(cls, participant: dict[str, Any], target: str) -> bool:
        target_alias = cls._normalize_group_agent_alias(target)
        if not target_alias:
            return False
        generic = {"agent", "assistant", "main", "group", "member", "成员", "群成员", "主模型"}
        if target_alias in generic:
            return False
        aliases = cls._group_agent_participant_aliases(participant)
        if target_alias in aliases:
            return True
        target_tokens = cls._group_agent_alias_tokens(target_alias)
        if target_tokens & aliases:
            return True
        target_compact = target_alias.replace(" ", "")
        for alias in aliases:
            alias_compact = alias.replace(" ", "")
            if len(target_compact) >= 3 and target_compact in alias_compact:
                return True
            if len(alias_compact) >= 3 and alias_compact in target_compact:
                return True
        return False

    def _resolve_group_dispatch_runnable(
        self,
        service: Any,
        context: dict[str, Any],
        directive: GroupDispatchDirective,
    ) -> dict[str, Any] | None:
        runnable = service.resolve_runnable(
            runnable_id=directive.runnable_id,
            name=directive.target,
        )
        if runnable is not None:
            return runnable
        target = directive.target_label.strip()
        if not target:
            return None
        participants = [
            item
            for item in (context.get("participants") or [])
            if isinstance(item, dict) and item.get("kind") == "agent"
        ]
        matched = [
            participant
            for participant in participants
            if self._group_agent_participant_matches_target(participant, target)
        ]
        if len(matched) > 1:
            raise AgentRuntimeError("群组 Agent 名称不唯一")
        if not matched:
            return None
        participant_id = str(matched[0].get("id") or "").strip()
        if not participant_id:
            return None
        return service.resolve_runnable(runnable_id=participant_id)

    def _dispatch_group_agent_requests(
        self,
        assistant_message: ChatMessage,
        requests: list[GroupDispatchDirective | dict[str, str]],
        context: dict[str, Any],
        *,
        source_text: str = "",
        notify_group_summary: bool = True,
    ) -> None:
        service = self._agent_runtime_service()
        directives = [self._coerce_group_dispatch_directive(request) for request in requests]
        resolved: list[tuple[GroupDispatchDirective, dict[str, Any]]] = []
        skipped: list[str] = []
        for directive in directives:
            try:
                runnable = self._resolve_group_dispatch_runnable(service, context, directive)
            except AgentRuntimeError as exc:
                skipped.append(f"{directive.target_label}: {exc}")
                continue
            if directive.kind == "workflow" or (runnable is not None and runnable.get("kind") == "workflow"):
                label = str(
                    directive.target
                    or (runnable or {}).get("name")
                    or directive.runnable_id
                    or "Workflow"
                ).strip()
                skipped.append(f"{label}: Workflow 不能在群聊派发中直接执行，请到 Agent Studio 的 Workflow Studio 或 Runs 面板运行")
                continue
            if runnable is None or runnable.get("kind") != "agent":
                skipped.append(f"{directive.target_label}: 未找到群组 Agent")
                continue
            if not self._group_context_contains_runnable(context, runnable, directive):
                skipped.append(f"{directive.target or runnable.get('name')}: 不在当前群组中")
                continue
            resolved.append((directive, runnable))

        summary = self._format_group_dispatch_summary(resolved, skipped)
        dispatch_metadata = self._message_metadata(assistant_message.message_id)
        if (
            dispatch_metadata.get("group_dispatch_state") in {"prepared", "started"}
            and "group_dispatch_source_intro" in dispatch_metadata
        ):
            dispatch_source = str(
                dispatch_metadata.get("group_dispatch_source_intro") or ""
            )
        else:
            dispatch_source = source_text or assistant_message.content
        source_intro = self._normalize_group_dispatch_intro(
            self._strip_group_dispatch_payloads(dispatch_source)
        ).strip()
        visible_content = self._format_group_dispatch_visible_content(
            dispatch_source,
            summary,
        )
        resolved_names = [
            str(runnable.get("nickname") or runnable.get("name") or directive.target or "Agent").strip()
            for directive, runnable in resolved
        ]
        resolved_names = [name for name in resolved_names if name]
        prepared_directives = [
            {
                "kind": "agent",
                "target": str(
                    runnable.get("nickname")
                    or runnable.get("name")
                    or directive.target
                    or ""
                ).strip(),
                "runnable_id": str(runnable.get("id") or "").strip(),
                "goal": directive.goal,
            }
            for directive, runnable in resolved
        ]
        self._session.update_assistant_message(
            assistant_message.message_id,
            visible_content,
            status=MessageStatus.COMPLETED,
            metadata={
                "sender": self._main_model_sender_from_runtime(),
                "group_dispatch_state": "prepared" if resolved else "handled",
                "group_dispatch_prepared": prepared_directives,
                "group_dispatch_source_intro": source_intro,
                "group_dispatch_handled": True if not resolved else None,
                "group_dispatch_count": len(resolved),
                "group_dispatch_skipped": skipped,
            },
        )
        if not resolved:
            self._record_group_dispatch_activity(
                task_id=assistant_message.task_id or "",
                title="群组任务派发失败",
                detail="没有找到可接收任务的 Agent",
                status="failed",
                event_id=f"{assistant_message.task_id or assistant_message.message_id}-group-dispatch-complete",
            )
            if skipped:
                if notify_group_summary:
                    self._maybe_create_group_agent_summary_task(assistant_message.task_id or "")
            return

        # Group sessions are long-lived; every main-model dispatch starts a
        # fresh run group, while all Agents in the same dispatch share it.
        run_group_id = ""
        root_run_id = ""
        next_context = dict(context)
        dispatch_lock = threading.Lock()
        dispatch_state: dict[str, Any] = {
            "closed": False,
            "started_count": 0,
            "completed_runs": [],
        }
        parent_metadata = self._message_metadata(assistant_message.message_id)
        persisted_started = parent_metadata.get("group_dispatch_started")
        started_receipts = [
            dict(item)
            for item in (persisted_started if isinstance(persisted_started, list) else [])
            if isinstance(item, dict)
        ]

        def _project_dispatch_group_if_settled() -> None:
            with dispatch_lock:
                if (
                    not dispatch_state["closed"]
                    or dispatch_state["started_count"] <= 0
                    or not dispatch_state["completed_runs"]
                ):
                    return
                completed_run = dict(dispatch_state["completed_runs"][-1])
                expected_member_count = int(dispatch_state["started_count"])
            project_group_terminal_after_member(
                service,
                completed_run,
                expected_member_count=expected_member_count,
            )

        for dispatch_index, (directive, runnable) in enumerate(resolved):
            sender = self._participant_for_runnable(runnable)
            initial_content = ""
            child_identity = normalize_run_group_child_identity(
                "chat-group-dispatch:"
                f"{assistant_message.task_id or assistant_message.message_id}:"
                f"{dispatch_index}:{runnable.get('id') or ''}"
            )
            assistant_id = self._session.upsert_assistant_projection_message(
                child_identity,
                initial_content,
                status=MessageStatus.PROCESSING,
                metadata={
                    "sender": sender,
                    "runnable_kind": "agent",
                    "runnable_id": runnable.get("id") or "",
                    "run_group_id": run_group_id,
                    "run_status": "processing",
                    "conversation_kind": "group",
                    "group_goal": directive.goal,
                    "delegated_by_task_id": assistant_message.task_id or "",
                    "delegated_goal": directive.goal,
                },
            )
            callback_session_id = self._session.session_id

            def _on_run_complete(
                run_result: dict[str, Any],
                *,
                message_id: str = assistant_id,
                current_sender: dict[str, Any] = sender,
                session_id: str = callback_session_id,
            ) -> None:
                self._with_existing_session(
                    session_id,
                    lambda: self._update_agent_run_message_from_result(
                        message_id,
                        current_sender,
                        run_result,
                        notify_group_summary=notify_group_summary,
                    ),
                )
                with dispatch_lock:
                    dispatch_state["completed_runs"].append(dict(run_result))
                _project_dispatch_group_if_settled()

            try:
                if run_group_id and not root_run_id:
                    raise AgentRuntimeError("group_dispatch_parent_run_missing")
                run_group_attachment = (
                    issue_run_group_child_attachment(
                        run_group_id=run_group_id,
                        parent_run_id=root_run_id,
                        child_kind="agent_run",
                        child_runnable_id=str(runnable.get("id") or ""),
                        child_identity=child_identity,
                    )
                    if run_group_id
                    else None
                )
                run = create_runnable_run(
                    service,
                    runnable_id=str(runnable.get("id") or ""),
                    user_goal=directive.goal,
                    run_group_id=run_group_id,
                    client_run_id=child_identity,
                    upstream=self._with_group_context_for_agent_upstream(
                        self._chat_upstream_context(),
                        next_context,
                        sender,
                    ),
                    project_root_group=False,
                    run_group_attachment=run_group_attachment,
                    on_complete=_on_run_complete,
                )
            except AgentRuntimeError as exc:
                agent_report = redact_api_error_text(exc)
                content = self._group_delegated_agent_terminal_content(
                    sender,
                    "failed",
                    directive.goal,
                    agent_report,
                )
                self._session.update_assistant_message(
                    assistant_id,
                    content,
                    status=MessageStatus.FAILED,
                    error=content,
                    metadata={
                        "run_status": "failed",
                        "agent_report": agent_report,
                        "agent_report_status": "failed",
                    },
                )
                if notify_group_summary:
                    self._maybe_create_group_agent_summary_task(assistant_message.task_id or "")
                continue

            run_group_id = str(run.get("run_group_id") or run_group_id)
            if not root_run_id:
                root_run_id = str(run.get("run_id") or "")
            with dispatch_lock:
                dispatch_state["started_count"] += 1
                if self._normalize_agent_run_status(str(run.get("status") or "")) in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    run_id = str(run.get("run_id") or "")
                    if not any(
                        str(item.get("run_id") or "") == run_id
                        for item in dispatch_state["completed_runs"]
                    ):
                        dispatch_state["completed_runs"].append(dict(run))
            started_receipt = {
                "dispatch_index": dispatch_index,
                "runnable_id": str(runnable.get("id") or ""),
                "child_identity": child_identity,
                "run_id": str(run.get("run_id") or ""),
                "run_group_id": run_group_id,
            }
            started_receipts = [
                item
                for item in started_receipts
                if str(item.get("child_identity") or "") != child_identity
            ]
            started_receipts.append(started_receipt)
            self._session.update_assistant_message(
                assistant_message.message_id,
                visible_content,
                status=MessageStatus.COMPLETED,
                metadata={
                    "group_dispatch_state": "started",
                    "group_dispatch_started": started_receipts,
                    "group_dispatch_run_group_id": run_group_id,
                },
            )
            self._attach_processing_agent_run_metadata(assistant_id, initial_content, run)
            if run_group_id:
                next_context["run_group_id"] = run_group_id
                self._bind_group_session_context(next_context, run_group_id=run_group_id)

        with dispatch_lock:
            dispatch_state["closed"] = True
        _project_dispatch_group_if_settled()

        self._record_group_dispatch_activity(
            task_id=assistant_message.task_id or "",
            title="群组任务已派发" if dispatch_state["started_count"] else "群组任务派发失败",
            detail="、".join(resolved_names) if dispatch_state["started_count"] else "没有找到可接收任务的 Agent",
            status="completed" if dispatch_state["started_count"] else "failed",
            event_id=f"{assistant_message.task_id or assistant_message.message_id}-group-dispatch-complete",
        )
        self._session.update_assistant_message(
            assistant_message.message_id,
            visible_content,
            status=MessageStatus.COMPLETED,
            metadata={
                "group_dispatch_state": "handled",
                "group_dispatch_handled": True,
                "group_dispatch_run_group_id": run_group_id or None,
                "group_dispatch_started": started_receipts,
            },
        )

    def _maybe_create_group_agent_summary_task(self, parent_task_id: str) -> None:
        parent_task_id = str(parent_task_id or "").strip()
        if not parent_task_id:
            return
        with _GROUP_AGENT_SUMMARY_LOCK:
            context = self._session_context()
            if context.get("conversation_kind") != "group":
                return
            parent = self._session.get_assistant_message_for_task(parent_task_id)
            if parent is None:
                return
            parent_metadata = parent.metadata if isinstance(parent.metadata, dict) else {}
            if parent_metadata.get("group_agent_summary_task_id"):
                return
            children = self._delegated_group_agent_children(parent_task_id)
            skipped = parent_metadata.get("group_dispatch_skipped")
            has_skipped = isinstance(skipped, list) and any(str(item or "").strip() for item in skipped)
            expected_count = int(parent_metadata.get("group_dispatch_count") or 0)
            if not children and not has_skipped:
                return
            if expected_count and len(children) < expected_count:
                return
            if any(not self._is_terminal_delegated_agent_message(child) for child in children):
                return

            task = self._state.create_task(
                task_type=TaskType.GENERAL,
                description="Summarize the supplied context.",
                response_context=self._group_agent_summary_task_description(parent, children),
                chat_session_id=self._session.session_id,
            )
            self._session.upsert_assistant_message(
                task_id=task.task_id,
                content="",
                status=MessageStatus.PROCESSING,
                metadata={
                    "sender": self._main_model_sender_from_runtime(),
                    "group_agent_summary_for_task_id": parent_task_id,
                    "group_dispatch_handled": True,
                },
            )
            self._session.update_assistant_message(
                parent.message_id,
                parent.content,
                status=parent.status,
                error=parent.error,
                metadata={
                    "group_agent_summary_task_id": task.task_id,
                    "group_agent_summary_pending": True,
                },
            )

    def _delegated_run_summary_message(self, run_id: str) -> ChatMessage | None:
        run_id = str(run_id or "").strip()
        if not run_id:
            return None
        for msg in self._session.get_all_messages():
            metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            if str(metadata.get("delegated_run_summary_for_run_id") or "").strip() == run_id:
                return msg
        return None

    def _delegated_run_activity(self, run_id: str) -> dict[str, Any] | None:
        run_id = str(run_id or "").strip()
        if not run_id:
            return None
        try:
            events = self._activity_store().list_events(
                session_id=self._session.session_id,
                query=run_id,
                tool="oha.delegation",
                phase="subagent",
                limit=50,
                key_only=True,
            )
        except Exception:
            logger.debug("读取自动委派 activity 失败: run_id=%s", run_id, exc_info=True)
            return None
        for event in events:
            event_dict = event.to_dict()
            metadata = event_dict.get("metadata") if isinstance(event_dict.get("metadata"), dict) else {}
            if str(metadata.get("run_id") or "").strip() == run_id:
                return event_dict
        return None

    def _delegated_run_summary_task_description(self, run: dict[str, Any], activity: dict[str, Any]) -> str:
        source_task_id = str(activity.get("task_id") or "").strip()
        user_request = ""
        main_reply = ""
        if source_task_id:
            for msg in self._session.get_all_messages():
                if msg.task_id != source_task_id:
                    continue
                if msg.role == MessageRole.USER and not user_request:
                    user_request = str(msg.content or "").strip()
                elif msg.role == MessageRole.ASSISTANT and not main_reply:
                    main_reply = str(msg.content or "").strip()
        status = self._normalize_agent_run_status(str(run.get("status") or ""))
        runnable_name = str(run.get("runnable_name") or run.get("runnable_id") or "Yachiyo Agent").strip() or "Yachiyo Agent"
        goal = str(run.get("user_goal") or "").strip()
        result = str(run.get("result") or "").strip()
        artifact_count, artifact_summaries = self._visible_run_artifact_summaries(run)

        lines = [
            "[Oha-Yachiyo 自动委派 Run 汇总]",
            "你是当前对话的主模型。你之前自动委派了一个 Agent/Workflow Run，现在它已经结束，请把结果整理后回复用户。",
            "不要再输出 oha_delegation 或任何机器可读委派 JSON；如果还需要继续委派，请先用自然语言说明需要用户确认。",
            "回复需要说明：委派目标完成/失败/取消了什么、关键结果是什么、是否有产物，以及用户下一步可以验收或继续做什么。",
        ]
        if user_request:
            lines.extend(["", f"用户原始请求：{user_request}"])
        if main_reply:
            lines.extend(["", f"你之前对用户的回复：{self._strip_oha_delegation_payloads(main_reply)}"])
        activity_title = str(activity.get("title") or "").strip()
        activity_detail = str(activity.get("detail") or "").strip()
        if activity_title or activity_detail:
            lines.extend(["", "委派活动："])
            if activity_title:
                lines.append(f"- {activity_title}")
            if activity_detail:
                lines.append(f"- {_compact_preview(activity_detail, 500)}")
        lines.extend(["", "Run 结果：", f"- {runnable_name}：{self._workflow_status_label(status)}"])
        if goal:
            lines.append(f"  任务：{goal}")
        if result:
            lines.append(f"  汇报：{result}")
        evidence_lines = self._run_execution_evidence_lines(run)
        if evidence_lines:
            lines.append("  执行线索：")
            lines.extend(f"  - {item}" for item in evidence_lines)
        if artifact_summaries:
            artifact_parts = [
                f"{item.get('path')} ({item.get('kind')})" if item.get("kind") else str(item.get("path") or "")
                for item in artifact_summaries
                if item.get("path")
            ]
            extra_count = max(0, artifact_count - len(artifact_parts))
            extra_note = f"；另有 {extra_count} 个产物见 Run Detail" if extra_count else ""
            lines.append(f"  产物：{'; '.join(artifact_parts)}{extra_note}")
        return "\n".join(lines)

    def _maybe_create_group_direct_agent_summary_task(self, message_id: str) -> None:
        message_id = str(message_id or "").strip()
        if not message_id:
            return
        with _GROUP_AGENT_SUMMARY_LOCK:
            context = self._session_context()
            if context.get("conversation_kind") != "group":
                return
            agent_message = next(
                (msg for msg in self._session.get_all_messages() if msg.message_id == message_id),
                None,
            )
            if agent_message is None or agent_message.role != MessageRole.ASSISTANT:
                return
            metadata = agent_message.metadata if isinstance(agent_message.metadata, dict) else {}
            if metadata.get("delegated_by_task_id"):
                return
            if str(metadata.get("runnable_kind") or "") != "agent":
                return
            if metadata.get("group_agent_summary_task_id"):
                return
            if str(metadata.get("run_status") or "").strip() not in {"completed", "failed", "cancelled"}:
                return

            task = self._state.create_task(
                task_type=TaskType.GENERAL,
                description="Summarize the supplied context.",
                response_context=self._group_direct_agent_summary_task_description(agent_message),
                chat_session_id=self._session.session_id,
            )
            self._session.upsert_assistant_message(
                task_id=task.task_id,
                content="",
                status=MessageStatus.PROCESSING,
                metadata={
                    "sender": self._main_model_sender_from_runtime(),
                    "group_direct_agent_summary_for_message_id": message_id,
                    "group_dispatch_handled": True,
                },
            )
            self._session.update_assistant_message(
                agent_message.message_id,
                agent_message.content,
                status=agent_message.status,
                error=agent_message.error,
                metadata={
                    "group_agent_summary_task_id": task.task_id,
                    "group_agent_summary_pending": True,
                },
            )

    def _create_pending_group_agent_summary_tasks(self) -> None:
        context = self._session_context()
        if context.get("conversation_kind") != "group":
            return
        delegated_parent_ids: set[str] = set()
        direct_message_ids: list[str] = []
        for msg in self._session.get_all_messages():
            if msg.role != MessageRole.ASSISTANT:
                continue
            metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            if str(metadata.get("runnable_kind") or "") != "agent":
                continue
            if metadata.get("group_agent_summary_task_id"):
                continue
            run_status = str(metadata.get("run_status") or "").strip()
            if run_status not in {"completed", "failed", "cancelled"}:
                continue
            parent_task_id = str(metadata.get("delegated_by_task_id") or "").strip()
            if parent_task_id:
                delegated_parent_ids.add(parent_task_id)
            elif (
                str(metadata.get("conversation_kind") or "") == "group"
                or bool(metadata.get("group_goal"))
                or bool(metadata.get("source_message_id"))
            ):
                direct_message_ids.append(msg.message_id)
        for parent_task_id in delegated_parent_ids:
            self._maybe_create_group_agent_summary_task(parent_task_id)
        for message_id in direct_message_ids:
            self._maybe_create_group_direct_agent_summary_task(message_id)

    def _sync_group_agent_summary_parent_statuses(self) -> None:
        context = self._session_context()
        if context.get("conversation_kind") != "group":
            return
        all_messages = self._session.get_all_messages()
        for summary in all_messages:
            if summary.role != MessageRole.ASSISTANT or not summary.task_id:
                continue
            summary_metadata = summary.metadata if isinstance(summary.metadata, dict) else {}
            parent_task_id = str(summary_metadata.get("group_agent_summary_for_task_id") or "").strip()
            direct_message_id = str(summary_metadata.get("group_direct_agent_summary_for_message_id") or "").strip()
            if summary.status not in (MessageStatus.COMPLETED, MessageStatus.FAILED):
                continue
            if parent_task_id:
                parent = self._session.get_assistant_message_for_task(parent_task_id)
            elif direct_message_id:
                parent = next(
                    (msg for msg in all_messages if msg.message_id == direct_message_id),
                    None,
                )
            else:
                parent = next(
                    (
                        msg
                        for msg in all_messages
                        if msg.role == MessageRole.ASSISTANT
                        and msg.message_id != summary.message_id
                        and isinstance(msg.metadata, dict)
                        and str(msg.metadata.get("group_agent_summary_task_id") or "") == summary.task_id
                    ),
                    None,
                )
            if parent is None:
                continue
            parent_metadata = parent.metadata if isinstance(parent.metadata, dict) else {}
            if str(parent_metadata.get("group_agent_summary_task_id") or "") != summary.task_id:
                continue
            if not parent_metadata.get("group_agent_summary_pending"):
                continue

            cleaned_metadata = dict(parent_metadata)
            cleaned_metadata["group_agent_summary_pending"] = None
            summary_status = summary.status.value if isinstance(summary.status, MessageStatus) else str(summary.status or "")
            if summary_status == MessageStatus.FAILED.value:
                cleaned_metadata["group_agent_summary_status"] = (
                    "cancelled" if self._message_is_cancelled_task_notice(summary) else "failed"
                )
            else:
                cleaned_metadata["group_agent_summary_status"] = "completed"
            if summary.error:
                cleaned_metadata["group_agent_summary_error"] = summary.error
            else:
                cleaned_metadata.pop("group_agent_summary_error", None)
            if parent_task_id:
                self._session.update_assistant_message(
                    parent.message_id,
                    content=parent.content,
                    status=parent.status,
                    error=parent.error,
                    metadata=cleaned_metadata,
                )
            elif direct_message_id:
                cleaned_metadata["group_agent_summary_pending"] = False
                self._session.update_assistant_message(
                    parent.message_id,
                    parent.content,
                    status=parent.status,
                    error=parent.error,
                    metadata=cleaned_metadata,
                )
            else:
                self._session.update_assistant_message(
                    parent.message_id,
                    content=parent.content,
                    status=parent.status,
                    error=parent.error,
                    metadata=cleaned_metadata,
                )

    @staticmethod
    def _message_is_cancelled_task_notice(message: ChatMessage) -> bool:
        text = str(message.error or message.content or "").strip()
        return text == "任务已取消" or text == "⚠️ 任务已取消"

    def _delegated_group_agent_children(self, parent_task_id: str) -> list[ChatMessage]:
        return [
            msg
            for msg in self._session.get_all_messages()
            if msg.role == MessageRole.ASSISTANT
            and isinstance(msg.metadata, dict)
            and msg.metadata.get("delegated_by_task_id") == parent_task_id
            and msg.metadata.get("runnable_kind") == "agent"
        ]

    def _group_direct_agent_summary_task_description(self, agent_message: ChatMessage) -> str:
        metadata = agent_message.metadata if isinstance(agent_message.metadata, dict) else {}
        sender = metadata.get("sender") if isinstance(metadata.get("sender"), dict) else {}
        name = str(sender.get("nickname") or sender.get("name") or "Agent").strip() or "Agent"
        status = str(metadata.get("agent_report_status") or metadata.get("run_status") or agent_message.status.value).strip()
        goal = str(metadata.get("group_goal") or "").strip()
        report = str(metadata.get("agent_report") or agent_message.error or agent_message.content or "").strip()
        source_message_id = str(metadata.get("source_message_id") or "").strip()
        user_request = ""
        request_message_id = ""
        messages = self._session.get_all_messages()
        for index, msg in enumerate(messages):
            if msg.message_id != agent_message.message_id:
                continue
            prior_messages = messages[:index]
            if source_message_id:
                source = next((item for item in prior_messages if item.message_id == source_message_id), None)
                if source is not None:
                    user_request = str(source.content or "").strip()
                    request_message_id = source.message_id
                    break
            source = next((item for item in reversed(prior_messages) if item.role == MessageRole.USER), None)
            if source is not None:
                user_request = str(source.content or "").strip()
                request_message_id = source.message_id
            break
        followups = self._group_followup_user_messages_after(
            request_message_id,
            agent_message_id=agent_message.message_id,
        )

        lines = [
            "[Oha-Yachiyo 群组直接 Agent 汇总]",
            "你是这个群组的主模型。用户刚刚直接点名了某个 Agent，Agent 已把执行结果交给你，请由你整理后回复用户。",
            "不要再派发新的 Agent 任务，不要输出 oha.group_dispatch / oha_group_dispatch 或任何机器可读派活 JSON。",
            "回复必须明确区分：成功项、失败/取消/拒绝项、失败原因、可验收内容/产物、用户下一步可选动作。",
            "如果有失败或取消，不要把已经完成的部分说成整体失败；先说明已完成内容，再说明未完成原因和可继续动作。",
        ]
        if user_request:
            lines.extend(["", f"用户原始请求：{user_request}"])
        if followups:
            lines.extend(["", "用户后续补充/纠偏："])
            lines.extend(f"- {item}" for item in followups)
        lines.extend(["", "Agent 汇报：", f"- {name}：{self._workflow_status_label(status)}"])
        if goal:
            lines.append(f"  任务：{goal}")
        if report:
            lines.append(f"  汇报：{report}")
        run_id = str(metadata.get("run_id") or metadata.get("agent_run_id") or "").strip()
        if run_id:
            try:
                run = self._agent_runtime_service().get_run(run_id)
            except Exception:
                run = {}
            evidence_lines = self._run_execution_evidence_lines(run) if run else []
            if evidence_lines:
                lines.append("  执行线索：")
                lines.extend(f"  - {item}" for item in evidence_lines)

        artifacts = metadata.get("run_artifacts") if isinstance(metadata.get("run_artifacts"), list) else []
        artifact_parts: list[str] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            path = _compact_preview(str(artifact.get("path") or ""), 180)
            kind = _compact_preview(str(artifact.get("kind") or ""), 80)
            if not path:
                continue
            artifact_parts.append(f"{path} ({kind})" if kind else path)
            if len(artifact_parts) >= 8:
                break
        if artifact_parts:
            artifact_count = int(metadata.get("run_artifact_count") or len(artifact_parts))
            extra_count = max(0, artifact_count - len(artifact_parts))
            extra_note = f"；另有 {extra_count} 个产物见 Run Detail" if extra_count else ""
            lines.append(f"  产物：{'; '.join(artifact_parts)}{extra_note}")
        return "\n".join(lines)

    @staticmethod
    def _is_terminal_delegated_agent_message(message: ChatMessage) -> bool:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        status = str(metadata.get("run_status") or "").strip()
        return status in {"completed", "failed", "cancelled"}

    def _group_agent_summary_task_description(self, parent: ChatMessage, children: list[ChatMessage]) -> str:
        user_request = ""
        request_message_id = ""
        for msg in reversed(self._session.get_all_messages()):
            if msg.role == MessageRole.USER and msg.task_id == parent.task_id:
                user_request = str(msg.content or "").strip()
                request_message_id = msg.message_id
                break
        followups = self._group_followup_user_messages_after(
            request_message_id,
            task_id=parent.task_id or "",
        )
        lines = [
            "[Oha-Yachiyo 群组 Agent 汇总]",
            "你是这个群组的主模型。群内 Agent 已把执行结果交给你，请由你整合后回复用户。",
            "不要再派发新的 Agent 任务，不要输出 oha.group_dispatch / oha_group_dispatch 或任何机器可读派活 JSON。",
            "回复必须明确区分：成功项、失败/取消/拒绝项、失败原因、未执行派活、可验收内容/产物、用户下一步可选动作。",
            "如果有的 Agent 成功、有的 Agent 失败或被拒绝，不要把整轮任务说成单纯成功或单纯失败；先说明已完成内容，再说明失败原因和可继续动作。",
        ]
        if user_request:
            lines.extend(["", f"用户原始请求：{user_request}"])
        if followups:
            lines.extend(["", "用户后续补充/纠偏："])
            lines.extend(f"- {item}" for item in followups)
        parent_content = self._strip_group_dispatch_payloads(parent.content)
        parent_content = self._normalize_group_dispatch_intro(parent_content)
        if parent_content:
            lines.extend(["", f"你之前对用户说明的计划：{parent_content}"])
        parent_metadata = parent.metadata if isinstance(parent.metadata, dict) else {}
        skipped = parent_metadata.get("group_dispatch_skipped")
        if isinstance(skipped, list):
            skipped_items = [
                _compact_preview(str(item or ""), 240)
                for item in skipped
                if str(item or "").strip()
            ]
            if skipped_items:
                lines.extend(["", "未执行派活："])
                lines.extend(f"- {item}" for item in skipped_items)
        lines.append("")
        lines.append("Agent 汇报：")
        if not children:
            lines.append("- 没有 Agent 实际执行；请说明未执行原因，并给用户一个可操作的下一步建议。")
        for child in children:
            metadata = child.metadata if isinstance(child.metadata, dict) else {}
            sender = metadata.get("sender") if isinstance(metadata.get("sender"), dict) else {}
            name = str(sender.get("nickname") or sender.get("name") or "Agent").strip() or "Agent"
            status = str(metadata.get("agent_report_status") or metadata.get("run_status") or child.status.value).strip()
            goal = _compact_preview(str(metadata.get("delegated_goal") or metadata.get("group_goal") or ""), 180)
            report = str(metadata.get("agent_report") or child.error or child.content or "").strip()
            lines.append(f"- {name}：{self._workflow_status_label(status)}")
            if goal:
                lines.append(f"  任务：{goal}")
            if report:
                lines.append(f"  汇报：{report}")
            run_id = str(metadata.get("run_id") or metadata.get("agent_run_id") or "").strip()
            if run_id:
                try:
                    run = self._agent_runtime_service().get_run(run_id)
                except Exception:
                    run = {}
                evidence_lines = self._run_execution_evidence_lines(run) if run else []
                if evidence_lines:
                    lines.append("  执行线索：")
                    lines.extend(f"  - {item}" for item in evidence_lines)
            artifacts = metadata.get("run_artifacts") if isinstance(metadata.get("run_artifacts"), list) else []
            artifact_parts: list[str] = []
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    continue
                path = _compact_preview(str(artifact.get("path") or ""), 180)
                kind = _compact_preview(str(artifact.get("kind") or ""), 80)
                if not path:
                    continue
                artifact_parts.append(f"{path} ({kind})" if kind else path)
                if len(artifact_parts) >= 8:
                    break
            if artifact_parts:
                artifact_count = int(metadata.get("run_artifact_count") or len(artifact_parts))
                extra_count = max(0, artifact_count - len(artifact_parts))
                extra_note = f"；另有 {extra_count} 个产物见 Run Detail" if extra_count else ""
                lines.append(f"  产物：{'; '.join(artifact_parts)}{extra_note}")
        return "\n".join(lines)

    def _group_followup_metadata_for_user_message(
        self,
        text: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if context.get("conversation_kind") != "group":
            return {}
        if not self._is_group_followup_text(text):
            return {}
        targets = self._active_group_followup_targets()
        metadata: dict[str, Any] = {}
        if targets.get("task_ids"):
            metadata["group_followup_for_task_ids"] = targets["task_ids"]
        if targets.get("agent_message_ids"):
            metadata["group_followup_for_agent_message_ids"] = targets["agent_message_ids"]
        return metadata

    def _active_group_followup_targets(self) -> dict[str, list[str]]:
        latest_task_id = ""
        latest_agent_message_id = ""
        for msg in self._session.get_all_messages():
            if msg.role != MessageRole.ASSISTANT:
                continue
            metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            if str(metadata.get("runnable_kind") or "") != "agent":
                continue
            run_status = self._normalize_agent_run_status(str(metadata.get("run_status") or ""))
            is_active = run_status in _ACTIVE_RUN_STATUSES or msg.status in {
                MessageStatus.PENDING,
                MessageStatus.PROCESSING,
            }
            is_pending_group_summary = (
                run_status in {"completed", "failed", "cancelled"}
                and not metadata.get("group_agent_summary_task_id")
            )
            delegated_by_task_id = str(metadata.get("delegated_by_task_id") or "").strip()
            if delegated_by_task_id:
                parent = self._session.get_assistant_message_for_task(delegated_by_task_id)
                parent_metadata = parent.metadata if parent is not None and isinstance(parent.metadata, dict) else {}
                if not is_active and not (
                    is_pending_group_summary
                    and not parent_metadata.get("group_agent_summary_task_id")
                ):
                    continue
                latest_task_id = delegated_by_task_id
                latest_agent_message_id = ""
                continue
            if (
                str(metadata.get("conversation_kind") or "") == "group"
                or bool(metadata.get("group_goal"))
                or bool(metadata.get("source_message_id"))
            ):
                if not is_active and not is_pending_group_summary:
                    continue
                latest_task_id = ""
                latest_agent_message_id = msg.message_id

        return {
            "task_ids": [latest_task_id] if latest_task_id else [],
            "agent_message_ids": [latest_agent_message_id] if latest_agent_message_id else [],
        }

    def _group_followup_user_messages_after(
        self,
        message_id: str,
        *,
        task_id: str = "",
        agent_message_id: str = "",
        limit: int = 6,
    ) -> list[str]:
        message_id = str(message_id or "").strip()
        if not message_id:
            return []
        task_id = str(task_id or "").strip()
        agent_message_id = str(agent_message_id or "").strip()
        result: list[str] = []
        collecting = False
        for msg in self._session.get_all_messages():
            if msg.message_id == message_id:
                collecting = True
                continue
            if not collecting or msg.role != MessageRole.USER:
                continue
            if not self._is_main_or_plain_group_user_message(msg):
                continue
            metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            tagged_task_ids = {
                str(item or "").strip()
                for item in metadata.get("group_followup_for_task_ids", [])
                if str(item or "").strip()
            } if isinstance(metadata.get("group_followup_for_task_ids"), list) else set()
            tagged_agent_message_ids = {
                str(item or "").strip()
                for item in metadata.get("group_followup_for_agent_message_ids", [])
                if str(item or "").strip()
            } if isinstance(metadata.get("group_followup_for_agent_message_ids"), list) else set()
            if tagged_task_ids or tagged_agent_message_ids:
                if not (
                    (task_id and task_id in tagged_task_ids)
                    or (agent_message_id and agent_message_id in tagged_agent_message_ids)
                ):
                    continue
            text = _compact_preview(str(msg.content or "").strip(), 240)
            if text and self._is_group_followup_text(text):
                result.append(text)
        if limit > 0 and len(result) > limit:
            return result[-limit:]
        return result

    @staticmethod
    def _is_main_or_plain_group_user_message(message: ChatMessage) -> bool:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        target = metadata.get("target") if isinstance(metadata.get("target"), dict) else {}
        target_kind = str(target.get("kind") or "").strip()
        runnable_kind = str(metadata.get("runnable_kind") or "").strip()
        return target_kind in {"", "main"} and runnable_kind in {"", "main"}

    @staticmethod
    def _is_group_followup_text(text: str) -> bool:
        value = " ".join(str(text or "").split()).strip()
        if not value:
            return False
        parsed_main_mention = ChatAPI._parse_main_model_mention(value)
        direct_main_mention = parsed_main_mention is not None and value.startswith("@")
        normalized = parsed_main_mention[1].strip() if direct_main_mention else value
        if re.match(
            r"^(?:另一个|新目标|新任务|新开|另外再|再做一个|再来一个|接下来|重新测试|测试一下|我想测试|帮我派|安排一下|派发)",
            normalized,
        ):
            return False
        if direct_main_mention:
            return bool(re.match(
                r"^(?:补充|追加|修正|纠正|更正|刚才|上面|当前|这个|这版|这次|最终整理|等等|等下|对了|还有一点|另外补充|注意|要求|把|将|改成|改为|调整|换成|不要|别|去掉|删掉|移除|保留|保持|加上|加个|再加|顺便|最后|验收|总结|汇总|整理时|输出时|结果里)",
                normalized,
            ))
        return True

    @classmethod
    def _group_context_contains_runnable(
        cls,
        context: dict[str, Any],
        runnable: dict[str, Any],
        directive: GroupDispatchDirective | dict[str, str],
    ) -> bool:
        directive = cls._coerce_group_dispatch_directive(directive)
        participants = [
            item
            for item in (context.get("participants") or [])
            if isinstance(item, dict) and item.get("kind") == "agent"
        ]
        if not participants:
            return True
        runnable_values = {
            str(runnable.get("id") or "").strip().lower(),
            str(runnable.get("name") or "").strip().lower(),
            str(runnable.get("nickname") or "").strip().lower(),
            directive.target.lower(),
            directive.runnable_id.lower(),
        }
        runnable_values.discard("")
        for participant in participants:
            participant_values = {
                str(participant.get("id") or "").strip().lower(),
                str(participant.get("name") or "").strip().lower(),
                str(participant.get("nickname") or "").strip().lower(),
            }
            participant_values.discard("")
            if participant_values & runnable_values:
                return True
        return False

    @staticmethod
    def _format_group_dispatch_summary(
        resolved: list[tuple[GroupDispatchDirective, dict[str, Any]]],
        skipped: list[str],
    ) -> str:
        if not resolved:
            if skipped:
                return "我没能找到可以接这个任务的群组 Agent。\n\n" + "\n".join(f"- {item}" for item in skipped)
            return "我暂时没有派出任务。"
        names = [
            str(runnable.get("nickname") or runnable.get("name") or directive.target or "Agent").strip()
            for directive, runnable in resolved
        ]
        names = [name for name in names if name]
        if len(names) == 1:
            text = f"我把这个任务派给 {names[0]} 了。"
        else:
            joined_names = "、".join(names)
            text = f"我把 {len(names)} 个任务分别派给 {joined_names} 了。"
        if skipped:
            text += "\n\n以下派活没有执行：\n" + "\n".join(f"- {item}" for item in skipped)
        return text

    @classmethod
    def _format_group_dispatch_visible_content(cls, source_text: str, summary: str) -> str:
        intro = cls._strip_group_dispatch_payloads(source_text)
        intro = cls._normalize_group_dispatch_intro(intro)
        if intro and summary:
            return f"{intro}\n\n{summary}"
        return summary or intro

    @classmethod
    def _strip_group_dispatch_payloads(cls, content: str) -> str:
        text = str(content or "")
        if not text.strip():
            return ""
        spans = cls._group_dispatch_payload_spans(text)
        if not spans:
            return text
        output: list[str] = []
        cursor = 0
        for start, end in spans:
            output.append(text[cursor:start])
            cursor = end
        output.append(text[cursor:])
        return "".join(output)

    @classmethod
    def _strip_oha_delegation_payloads(cls, content: str) -> str:
        text = str(content or "")
        if not text.strip():
            return ""
        text = re.sub(
            r"<\s*oha[\s_-]*delegation\b[^>]*>.*?</\s*oha[\s_-]*delegation\s*>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r"```(?:json)?\s*[^`]*(?:run_oha|oha_delegation|delegate_agent|delegate_workflow)[^`]*```",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r"^\s*\{[^\n{}]*(?:run_oha|oha_delegation|delegate_agent|delegate_workflow)[^\n{}]*\}\s*$",
            "",
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        return text.strip()

    @classmethod
    def _group_dispatch_payload_spans(cls, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = cls._unsupported_group_dispatch_tag_spans(text)
        ignored_spans = list(spans)
        for match in re.finditer(
            r"<\s*(?:oha|native)[\s_-]*group[\s_-]*dispatch\b[^>]*>\s*(.*?)\s*</\s*(?:oha|native)[\s_-]*group[\s_-]*dispatch\s*>",
            text,
            re.DOTALL | re.IGNORECASE,
        ):
            spans.append(match.span())
        for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE):
            if cls._parse_group_dispatch_directives(match.group(1)):
                spans.append(match.span())

        decoder = json.JSONDecoder()
        index = 0
        while index < len(text):
            ignored_end = next((end for start, end in ignored_spans if start <= index < end), None)
            if ignored_end is not None:
                index = max(index + 1, ignored_end)
                continue
            if text[index] not in "{[":
                index += 1
                continue
            try:
                payload, offset = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                index += 1
                continue
            if cls._group_dispatch_directives_from_payload(payload):
                spans.append((index, index + max(offset, 1)))
            index += max(offset, 1)
        partial_start = cls._partial_group_dispatch_payload_start(text)
        if partial_start is not None:
            spans.append((partial_start, len(text)))
        return cls._merge_spans(spans)

    @staticmethod
    def _unsupported_group_dispatch_tag_spans(text: str) -> list[tuple[int, int]]:
        source = str(text or "")
        supported_name = r"(?:oha|native)[\s_-]*group[\s_-]*dispatch\b"
        unsupported_open = (
            r"<\s*(?!"
            + supported_name
            + r")[a-z][\w\s_-]*group[\s_-]*dispatch\b[^>]*>"
        )
        unsupported_close = (
            r"</\s*(?!"
            + supported_name
            + r")[a-z][\w\s_-]*group[\s_-]*dispatch\s*>"
        )
        closed_pattern = unsupported_open + r".*?" + unsupported_close
        spans = [
            match.span()
            for match in re.finditer(
                closed_pattern,
                source,
                re.DOTALL | re.IGNORECASE,
            )
        ]
        for match in re.finditer(unsupported_open, source, re.DOTALL | re.IGNORECASE):
            if any(start <= match.start() < end for start, end in spans):
                continue
            close_match = re.search(unsupported_close, source[match.end():], re.DOTALL | re.IGNORECASE)
            if close_match:
                spans.append((match.start(), match.end() + close_match.end()))
            else:
                spans.append((match.start(), len(source)))
        return spans

    @staticmethod
    def _partial_group_dispatch_payload_start(text: str) -> int | None:
        candidates: list[int] = []
        tag_match = re.search(
            r"<\s*(?:oha|native)[\s_-]*group[\s_-]*dispatch\b",
            text,
            re.IGNORECASE,
        )
        if tag_match:
            candidates.append(tag_match.start())
        for match in re.finditer(r"(^|\n)\s*```(?:json)?\s*", text, re.IGNORECASE):
            tail = text[match.end():]
            stripped_tail = tail.strip()
            if (
                not stripped_tail
                or stripped_tail in {"{", "["}
                or re.match(r"^[\[{]\s*$", stripped_tail)
                or re.search(r"dispatch|oha\.group_dispatch|native\.group_dispatch|\"(?:action|tasks|agents?|goal)\"", tail, re.IGNORECASE)
            ):
                candidates.append(match.start())
        for match in re.finditer(r"(^|\n)(?P<prefix>\s*)[\[{]", text):
            start = match.start() + len(match.group(1))
            tail = text[start:]
            if re.search(r"dispatch|oha\.group_dispatch|native\.group_dispatch|\"(?:action|tasks|dispatches?|agents?|tool|goal)\"", tail, re.IGNORECASE):
                candidates.append(start)
        return min(candidates) if candidates else None

    @staticmethod
    def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not spans:
            return []
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        return merged

    @staticmethod
    def _normalize_group_dispatch_intro(content: str) -> str:
        lines = []
        for line in str(content or "").splitlines():
            clean = line.strip()
            if not clean:
                lines.append("")
                continue
            if clean in {"```", "```json"}:
                continue
            if re.fullmatch(
                r"</?\s*(?:oha|native)[\s_-]*group[\s_-]*dispatch\s*>",
                clean,
                re.IGNORECASE,
            ):
                continue
            lowered = clean.lower()
            if any(
                marker in lowered
                for marker in (
                    "派活协议",
                    "派发协议",
                    "机器派发协议",
                    "机器块",
                    "oha_group_dispatch",
                    "native_group_dispatch",
                    "oha.group_dispatch",
                    "native.group_dispatch",
                    "group.dispatch",
                    "dispatch_group_agent",
                )
            ):
                continue
            lines.append(line.rstrip())
        text = "\n".join(lines).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def _record_group_dispatch_activity(
        self,
        *,
        task_id: str,
        title: str,
        detail: str,
        status: str,
        event_id: str,
    ) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        try:
            self._activity_store().record_event(
                session_id=self._session.session_id,
                task_id=task_id,
                tool_name="oha.group_dispatch",
                phase="tool_complete" if status in {"completed", "failed"} else "tool_start",
                title=title,
                detail=detail,
                status=status,
                event_id=event_id,
            )
        except Exception:
            logger.debug("记录群组派活活动失败: %s", task_id, exc_info=True)

    def get_session_info(self) -> Dict[str, Any]:
        """获取会话元信息"""
        self._sync_current_session_status()
        processing_count = self._session_processing_count(self._session.session_id)
        approval_count = self._session_approval_count(self._session.session_id)
        return {
            "session_id": self._session.session_id,
            "session_context": self._session_context(),
            "message_count": self._session.message_count(),
            "is_processing": processing_count > 0,
            "processing_count": processing_count,
            "approval_count": approval_count,
            "pending_message_id": self._session.get_pending_message_id(),
        }

    def get_executor_info(self) -> Dict[str, Any]:
        image_input = get_native_image_input_capability()
        runner = getattr(self._runtime, "task_runner", None)
        if runner is None:
            return {
                "executor": "none",
                "available": False,
                "image_input": image_input,
                "reason": user_task_unavailable_reason(self._runtime),
            }
        executor_name = runner.executor.name
        available = bool(execution_capabilities(runner.executor).get("model"))
        payload = {
            "executor": executor_name,
            "available": available,
            "image_input": image_input,
        }
        if not available:
            payload["reason"] = user_task_unavailable_reason(self._runtime)
        return payload

    def list_sessions(self, limit: int = 20, query: str = "") -> Dict[str, Any]:
        """列出最近会话，包含当前空白会话。"""
        self._sync_current_session_status()
        store = self._chat_store()
        normalized_query = " ".join(str(query or "").split()).strip()
        current_session = self._runtime.chat_session
        current_session_id = current_session.session_id
        search_limit = limit if limit <= 0 else max(limit, 50)
        search_results = store.search_sessions(normalized_query, limit=search_limit) if normalized_query else []
        sessions = [] if normalized_query else store.list_sessions(limit=limit)
        session_items = []
        iterable_sessions = [result.session for result in search_results] if normalized_query else sessions
        search_by_session = {
            result.session.session_id: result
            for result in search_results
        }
        for session in iterable_sessions:
            messages = store.load_messages(session.session_id, limit=240)
            search_result = search_by_session.get(session.session_id)
            processing_count = self._session_processing_count(session.session_id, messages=messages)
            approval_count = self._session_approval_count(session.session_id, messages=messages)
            session_items.append({
                "session_id": session.session_id,
                "title": self._session_title(session.title, messages),
                **self._serialize_session_context(session),
                "created_at": session.created_at,
                "updated_at": self._session_updated_at(session.session_id, session.created_at, messages=messages),
                "message_count": session.message_count,
                "is_processing": processing_count > 0,
                "processing_count": processing_count,
                "approval_count": approval_count,
                "token_count": self._session_token_count(store, session.session_id, messages, session.message_count),
                "latest_activity": self._latest_activity_for_session(session.session_id),
                "latest_message_preview": self._session_latest_user_turn(messages),
                "latest_message_status": self._session_latest_status(messages),
                "search_match": self._session_search_match(search_result, normalized_query),
            })
        if not normalized_query and not any(item["session_id"] == current_session_id for item in session_items):
            stored_current = store.get_session(current_session_id)
            current_messages = store.load_messages(current_session_id, limit=240)
            current_title = self._session_title(stored_current.title if stored_current else "", current_messages)
            current_context = self._serialize_session_context(stored_current) if stored_current else self._session_context()
            processing_count = self._session_processing_count(current_session_id, messages=current_messages)
            approval_count = self._session_approval_count(current_session_id, messages=current_messages)
            session_items.insert(
                0,
                {
                    "session_id": current_session_id,
                    "title": current_title or "新对话",
                    **current_context,
                    "created_at": stored_current.created_at if stored_current else "",
                    "updated_at": self._session_updated_at(
                        current_session_id,
                        stored_current.created_at if stored_current else "",
                        messages=current_messages,
                    ),
                    "message_count": stored_current.message_count if stored_current else 0,
                    "is_processing": processing_count > 0,
                    "processing_count": processing_count,
                    "approval_count": approval_count,
                    "token_count": self._session_token_count(
                        store,
                        current_session_id,
                        current_messages,
                        stored_current.message_count if stored_current else 0,
                    ),
                    "latest_activity": self._latest_activity_for_session(current_session_id),
                    "latest_message_preview": self._session_latest_user_turn(current_messages),
                    "latest_message_status": self._session_latest_status(current_messages),
                    "search_match": None,
                },
            )
        return {
            "ok": True,
            "current_session_id": current_session_id,
            "sessions": session_items,
            "query": normalized_query,
        }

    def _serialize_session_context(self, session: Any | None) -> dict[str, Any]:
        context = self._session_context(session)
        return {
            "conversation_kind": context["conversation_kind"],
            "runnable_id": context["runnable_id"],
            "runnable_name": context["runnable_name"],
            "run_group_id": context["run_group_id"],
            "avatar_url": context["avatar_url"],
            "participants": context["participants"],
        }

    @staticmethod
    def _session_search_match(search_result: Any, query: str) -> dict[str, Any] | None:
        if search_result is None or not query:
            return None
        snippet = _search_snippet(str(getattr(search_result, "match_content", "") or ""), query)
        match_message_id = getattr(search_result, "match_message_id", None)
        if not snippet and not match_message_id:
            return {
                "kind": "session",
                "query": query,
                "snippet": "会话标题或 Session ID 匹配",
                "match_count": int(getattr(search_result, "match_count", 0) or 0),
            }
        return {
            "kind": "message" if match_message_id else "session",
            "query": query,
            "message_id": match_message_id,
            "role": getattr(search_result, "match_role", "") or "",
            "snippet": snippet,
            "created_at": getattr(search_result, "match_created_at", "") or "",
            "match_count": int(getattr(search_result, "match_count", 0) or 0),
        }

    @staticmethod
    def _session_title(stored_title: str, messages: list[Any]) -> str:
        from apps.core.chat_store import make_session_title, strip_leading_session_mentions
        from apps.core.title_generator import looks_like_title_prompt_echo

        title = (stored_title or "").strip()
        if title and not ChatAPI._looks_like_session_id_title(title) and not looks_like_title_prompt_echo(title):
            return strip_leading_session_mentions(title) or title
        for msg in messages:
            if getattr(msg, "role", "") == MessageRole.USER.value:
                generated = make_session_title(str(getattr(msg, "content", "") or ""))
                if generated:
                    return generated
        return ""

    @staticmethod
    def _looks_like_session_id_title(value: str) -> bool:
        return bool(re.fullmatch(r"[a-f0-9]{8,32}", (value or "").strip(), flags=re.IGNORECASE))

    def load_session(self, session_id: str) -> Dict[str, Any]:
        """切换到指定历史会话。"""
        if not session_id:
            return {"ok": False, "error": "session_id 不能为空"}
        try:
            with self._runtime_session_pointer_lock():
                self._runtime.switch_session(session_id)
                return {
                    "ok": True,
                    "session_id": session_id,
                    "message_count": self._runtime.chat_session.message_count(),
                }
        except Exception as exc:
            logger.error("切换会话失败: %s", exc)
            return {"ok": False, "error": redact_api_error_text(exc)}

    def clear_session(self) -> Dict[str, Any]:
        """创建新会话；旧会话的后台任务继续写回原 session。"""
        try:
            self._sync_task_status_to_messages()
            previous_session_id = self._session.session_id
            start_new_session = getattr(self._runtime, "start_new_session", None)
            if callable(start_new_session):
                session_id = start_new_session()
            else:
                self._session.clear()
                session_id = self._session.session_id
            logger.info("新会话已创建: %s -> %s", previous_session_id, session_id)
            return {
                "ok": True,
                "session_id": session_id,
                "previous_session_id": previous_session_id,
                "cancelled_tasks": 0,
            }
        except Exception as exc:
            logger.error("清空会话失败: %s", exc)
            return {"ok": False, "error": redact_api_error_text(exc)}

    def discard_empty_current_session(self, session_id: str = "") -> Dict[str, Any]:
        """Discard one captured empty session without rebinding a newer chat."""
        target_session_id = str(session_id or self._session.session_id or "").strip()
        if not target_session_id:
            return {"ok": False, "error": "会话不存在"}

        try:
            # A scoped send takes the same lease before it commits. Whichever
            # operation acquires it first decides whether the captured session
            # is non-empty or has already been removed.
            with self._session_mutation_lock(target_session_id):
                return self._discard_empty_session_locked(target_session_id)
        except Exception as exc:
            logger.error("丢弃空白会话失败: %s", exc)
            return {"ok": False, "error": redact_api_error_text(exc)}

    def _discard_empty_session_locked(self, target_session_id: str) -> Dict[str, Any]:
        store = self._chat_store()
        if store.get_session(target_session_id) is None:
            return {
                "ok": True,
                "discarded": False,
                "session_id": target_session_id,
            }

        def _discard_target() -> bool:
            if self._session_is_processing(target_session_id):
                return False

            stored_session = store.get_session(target_session_id)
            if stored_session is None or stored_session.conversation_kind == "group":
                return False

            messages = store.load_messages(target_session_id, limit=1)
            if messages:
                return False

            store.delete_session(target_session_id)
            _remove_attachment_session_dir(target_session_id)
            return True

        discarded = self._with_session(
            target_session_id,
            _discard_target,
            require_existing=False,
        )
        if not discarded:
            return {
                "ok": True,
                "discarded": False,
                "session_id": target_session_id,
            }

        remaining = store.list_sessions(limit=1)

        # Keep a session loaded by another request. Only replace the runtime
        # pointer when it still names the session that this call captured.
        with self._runtime_session_pointer_lock():
            current_session_id = str(
                self._runtime.chat_session.session_id or ""
            ).strip()
            if current_session_id == target_session_id:
                if remaining:
                    next_session_id = remaining[0].session_id
                    switch_session = getattr(self._runtime, "switch_session", None)
                    if not callable(switch_session):
                        raise RuntimeError("runtime 不支持切换会话")
                    switch_session(next_session_id)
                else:
                    self._runtime.chat_session.clear()
                    next_session_id = self._runtime.chat_session.session_id
            else:
                next_session_id = current_session_id

        logger.info("空白会话已丢弃: %s -> %s", target_session_id, next_session_id)
        return {
            "ok": True,
            "discarded": True,
            "deleted_session_id": target_session_id,
            "session_id": next_session_id,
            "empty": not remaining,
        }

    def cancel_current_tasks(self) -> Dict[str, Any]:
        """取消当前会话中仍在等待/执行的任务，但保留会话历史。"""
        try:
            self._sync_task_status_to_messages()
            cancelled_count = self._cancel_active_session_tasks("用户停止生成")
            messages = self.get_messages()
            return {
                "ok": True,
                "cancelled_tasks": cancelled_count,
                "session_id": self._session.session_id,
                "messages": messages.get("messages", []),
                "is_processing": messages.get("is_processing", False),
                "processing_count": messages.get("processing_count", 0),
            }
        except Exception as exc:
            logger.error("取消当前会话任务失败: %s", exc)
            return {"ok": False, "error": redact_api_error_text(exc)}

    def _task_progress_label(self, task_id: str | None) -> str:
        if not task_id:
            return ""
        task = self._state.get_task(task_id)
        return str(getattr(task, "progress_label", "") or "") if task is not None else ""

    def _activity_events_by_task(self, task_ids: list[str | None], limit_per_task: int = 5) -> dict[str, list[dict[str, Any]]]:
        ids = [task_id for task_id in task_ids if task_id]
        if not ids:
            return {}
        try:
            store = self._activity_store()
            result: dict[str, list[dict[str, Any]]] = {}
            for task_id, events in store.latest_by_task(ids, limit_per_task=limit_per_task, key_only=True).items():
                visible = [
                    event_dict
                    for event in events
                    if _is_chat_visible_activity(event_dict := event.to_dict())
                ]
                if visible:
                    result[task_id] = visible
            return result
        except Exception:
            logger.debug("读取任务活动事件失败", exc_info=True)
            return {}

    def _latest_activity_for_session(self, session_id: str) -> dict[str, Any]:
        try:
            events = self._activity_store().list_events(session_id=session_id, limit=1, key_only=True)
            return events[0].to_dict() if events else {}
        except Exception:
            logger.debug("读取会话最新活动失败", exc_info=True)
            return {}

    def _session_is_processing(self, session_id: str) -> bool:
        return self._session_processing_count(session_id) > 0

    def _session_processing_count(self, session_id: str, messages: list[Any] | None = None) -> int:
        count = 0
        for task in self._state.list_tasks():
            if getattr(task, "chat_session_id", None) != session_id:
                continue
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                count += 1
        count += len(self._session_active_run_refs(session_id, messages=messages))
        return count

    def _session_approval_count(self, session_id: str, messages: list[Any] | None = None) -> int:
        approval_run_ids: set[str] = set()
        for run_id, (_msg, metadata, run) in self._session_active_run_refs(session_id, messages=messages).items():
            status = self._normalize_agent_run_status(str(run.get("status") or ""))
            if not status:
                status = self._normalize_agent_run_status(
                    str(metadata.get("run_status") or metadata.get("workflow_status") or "")
                )
            pending = run.get("pending_approval") if isinstance(run.get("pending_approval"), dict) else {}
            if not pending.get("tool") and isinstance(metadata.get("pending_approval"), dict):
                pending = metadata.get("pending_approval") or {}
            if status == "approval_required" and pending.get("tool"):
                approval_run_ids.add(run_id)
                continue
            if status == "approval_required" and self._workflow_waiting_for_child_approval(run):
                waiting_context = self._workflow_child_approval_context(run)
                child_run_id = str(waiting_context.get("child_run_id") or "").strip()
                if child_run_id:
                    approval_run_ids.add(child_run_id)
        return len(approval_run_ids)

    def _session_active_run_refs(
        self,
        session_id: str,
        messages: list[Any] | None = None,
    ) -> dict[str, tuple[Any, dict[str, Any], dict[str, Any]]]:
        try:
            if session_id == self._session.session_id:
                messages = self._session.get_all_messages()
            elif messages is None:
                messages = self._chat_store().load_messages(session_id, limit=240)
        except Exception:
            return {}

        candidates: dict[str, tuple[Any, dict[str, Any]]] = {}
        for msg in messages or []:
            metadata = getattr(msg, "metadata", None)
            if not isinstance(metadata, dict):
                continue
            run_id = str(metadata.get("run_id") or metadata.get("workflow_run_id") or "").strip()
            if not run_id:
                continue
            status = str(
                metadata.get("run_status")
                or metadata.get("workflow_status")
                or getattr(getattr(msg, "status", ""), "value", "")
                or getattr(msg, "status", "")
                or ""
            ).strip()
            normalized = self._normalize_agent_run_status(status)
            if normalized in _ACTIVE_RUN_STATUSES and run_id not in candidates:
                candidates[run_id] = (msg, metadata)
        if not candidates:
            return {}

        try:
            service = self._agent_runtime_service()
        except Exception:
            return {}
        if not hasattr(service, "get_run"):
            return {
                run_id: (msg, metadata, {})
                for run_id, (msg, metadata) in candidates.items()
            }

        active: dict[str, tuple[Any, dict[str, Any], dict[str, Any]]] = {}
        for run_id, (msg, metadata) in candidates.items():
            try:
                run = service.get_run(run_id)
            except Exception:
                continue
            status = self._normalize_agent_run_status(str(run.get("status") or ""))
            if status in _ACTIVE_RUN_STATUSES:
                active[run_id] = (msg, metadata, run)
        return active

    def _session_updated_at(self, session_id: str, fallback: str = "", messages: list[Any] | None = None) -> str:
        try:
            messages = messages if messages is not None else self._chat_store().load_messages(session_id, limit=240)
        except Exception:
            return fallback
        latest = messages[-1].created_at if messages else fallback
        activity = self._latest_activity_for_session(session_id)
        activity_time = str(activity.get("created_at") or "")
        return max([value for value in (latest, activity_time, fallback) if value] or [""])

    def _session_latest_user_turn(self, messages: list[Any]) -> str:
        for msg in reversed(messages):
            if getattr(msg, "role", "") == MessageRole.USER.value and str(getattr(msg, "content", "") or "").strip():
                return _compact_preview(getattr(msg, "content", ""))
        for msg in reversed(messages):
            if str(getattr(msg, "content", "") or "").strip():
                return _compact_preview(getattr(msg, "content", ""))
        return ""

    def _session_latest_status(self, messages: list[Any]) -> str:
        for msg in reversed(messages):
            status = str(getattr(msg, "status", "") or "")
            if status:
                return status
        return ""

    @staticmethod
    def _session_token_count(store: Any, session_id: str, messages: list[Any], message_count: int) -> int:
        try:
            if int(message_count or 0) > len(messages):
                messages = store.load_messages(session_id, limit=0)
        except Exception:
            logger.debug("读取完整会话消息以估算 token 失败: %s", session_id, exc_info=True)
        return estimate_chat_tokens(messages)

    def delete_current_session(self) -> Dict[str, Any]:
        """删除当前会话，并切换到剩余最近会话或新建空会话。"""
        return self._delete_session_with_lease(self._session.session_id)

    def delete_session(self, session_id: str = "") -> Dict[str, Any]:
        """Delete one captured session without rebinding a newer active chat."""
        target_session_id = str(session_id or self._session.session_id or "").strip()
        return self._delete_session_with_lease(target_session_id)

    def _delete_session_with_lease(self, target_session_id: str) -> Dict[str, Any]:
        target_session_id = str(target_session_id or "").strip()
        if not target_session_id:
            return {"ok": False, "error": "会话不存在"}

        # This lease is acquired before reading messages/tasks. A scoped send
        # holds the same lease outside its idempotency singleflight lock, so the
        # delete observes either no commit or the complete message/task link.
        with self._session_mutation_lock(target_session_id):
            return self._delete_session_locked(target_session_id)

    def _delete_session_locked(self, target_session_id: str) -> Dict[str, Any]:
        try:
            store = self._chat_store()
            if store.get_session(target_session_id) is None:
                return {"ok": False, "error": "会话不存在"}

            def _delete_target() -> int:
                self._sync_task_status_to_messages()
                cancelled_count = self._cancel_active_session_tasks(
                    "删除会话前取消仍在执行的任务"
                )
                store.delete_session(target_session_id)
                _remove_attachment_session_dir(target_session_id)
                return cancelled_count

            # The thread-local override remains A even if another window loads
            # B while cancellation is in progress.
            cancelled_count = self._with_session(
                target_session_id,
                _delete_target,
                require_existing=False,
            )
            remaining = store.list_sessions(limit=1)
            remaining_count = store.count_sessions()

            # Pointer CAS is intentionally brief and happens only after the
            # potentially long send/cancel work. load_session uses the same
            # pointer lock, so B cannot be overwritten between check and swap.
            with self._runtime_session_pointer_lock():
                current_session_id = str(
                    self._runtime.chat_session.session_id or ""
                ).strip()
                if current_session_id == target_session_id:
                    if remaining:
                        next_session_id = remaining[0].session_id
                        switch_session = getattr(self._runtime, "switch_session", None)
                        if not callable(switch_session):
                            raise RuntimeError("runtime 不支持切换会话")
                        switch_session(next_session_id)
                    else:
                        self._runtime.chat_session.clear()
                        next_session_id = self._runtime.chat_session.session_id
                else:
                    next_session_id = current_session_id

            logger.info(
                "当前会话已删除: %s -> %s，已取消任务数=%d",
                target_session_id,
                next_session_id,
                cancelled_count,
            )
            return {
                "ok": True,
                "deleted_session_id": target_session_id,
                "session_id": next_session_id,
                "cancelled_tasks": cancelled_count,
                "remaining_sessions": remaining_count,
                "empty": not remaining,
            }
        except Exception as exc:
            logger.error("删除当前会话失败: %s", exc)
            return {"ok": False, "error": redact_api_error_text(exc)}

    def _cancel_active_session_tasks(self, reason: str) -> int:
        """取消当前会话中仍在等待/执行的任务，并持久化取消提示。"""
        active_task_ids: list[str] = []
        seen: set[str] = set()

        for msg in self._session.get_all_messages():
            if msg.role not in (MessageRole.USER, MessageRole.ASSISTANT):
                continue
            if msg.status not in (MessageStatus.PENDING, MessageStatus.PROCESSING):
                continue
            if not msg.task_id or msg.task_id in seen:
                continue
            task = self._state.get_task(msg.task_id)
            if task is None or task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
                continue
            seen.add(msg.task_id)
            active_task_ids.append(msg.task_id)

        # A task can cross AppState's commit boundary before both the primary
        # message link and its repair write fail. The persisted message then
        # has no task_id, so deletion must also discover active tasks by their
        # authoritative chat_session_id to avoid leaving a runner orphan.
        list_tasks = getattr(self._state, "list_tasks", None)
        if callable(list_tasks):
            try:
                session_id = self._session.session_id
                for task in list_tasks():
                    task_id = str(getattr(task, "task_id", "") or "").strip()
                    if (
                        not task_id
                        or task_id in seen
                        or str(getattr(task, "chat_session_id", "") or "").strip()
                        != session_id
                        or getattr(task, "status", None)
                        not in (TaskStatus.PENDING, TaskStatus.RUNNING)
                    ):
                        continue
                    seen.add(task_id)
                    active_task_ids.append(task_id)
            except Exception:
                logger.debug(
                    "按会话补扫活动任务失败: %s",
                    self._session.session_id,
                    exc_info=True,
                )

        cancelled = 0
        for task_id in active_task_ids:
            task = self._state.get_task(task_id)
            if task is None:
                continue
            did_cancel = False
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                try:
                    self._state.cancel_task(task_id)
                    cancel_runner_task = getattr(
                        self._runtime, "cancel_task_runner_task", None
                    )
                    if callable(cancel_runner_task):
                        cancel_runner_task(task_id)
                    cancelled += 1
                    did_cancel = True
                except (KeyError, ValueError):
                    logger.debug("任务取消跳过: %s", task_id, exc_info=True)

            task = self._state.get_task(task_id)
            if did_cancel and task is not None and task.status == TaskStatus.CANCELLED:
                try:
                    activity_store = self._activity_store()
                    activity_store.finalize_task_events(task_id, status="cancelled")
                    activity_store.record_event(
                        session_id=self._session.session_id,
                        task_id=task_id,
                        tool_name="native_agent",
                        phase="task_cancelled",
                        title="Yachiyo 已停止",
                        detail=reason,
                        status="cancelled",
                    )
                except Exception:
                    logger.debug("收尾取消任务活动事件失败: %s", task_id, exc_info=True)
                error = "任务已取消"
                self._session.upsert_assistant_message(
                    task_id=task_id,
                    content=f"⚠️ {error}",
                    status=MessageStatus.FAILED,
                    error=error,
                )

        cancelled += self._cancel_active_session_runs()
        return cancelled

    def _cancel_active_session_runs(self) -> int:
        """取消当前会话中挂在消息上的 Agent/Workflow Run。"""
        active_runs = self._session_active_run_refs(self._session.session_id)
        if not active_runs:
            return 0
        try:
            service = self._agent_runtime_service()
        except Exception:
            logger.debug("取消会话 Run 失败：无法取得 Agent Runtime Service", exc_info=True)
            return 0
        cancel_run = getattr(service, "cancel_run", None)
        if not callable(cancel_run):
            return 0

        cancelled = 0
        for run_id, (msg, metadata, _run) in active_runs.items():
            try:
                result = cancel_run(run_id)
            except Exception:
                logger.debug("取消会话 Run 跳过: %s", run_id, exc_info=True)
                continue
            status = self._normalize_agent_run_status(str(result.get("status") or ""))
            if status in _ACTIVE_RUN_STATUSES:
                continue
            cancelled += 1
            message_id = str(getattr(msg, "message_id", "") or "").strip()
            if not message_id:
                continue
            sender = metadata.get("sender") if isinstance(metadata.get("sender"), dict) else {}
            self._update_agent_run_message_from_result(
                message_id,
                sender,
                result,
                notify_group_summary=False,
            )
        return cancelled
