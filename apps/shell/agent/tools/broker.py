"""Controlled tool broker for custom API agent runs."""

from __future__ import annotations

import fnmatch
import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from apps.shell.agent.runtime.errors import AgentRuntimeError, AgentWorkspaceBoundaryError
from apps.shell.agent.tools import browser, desktop
from apps.shell.agent.tools.data_analysis import analyze_data_file, analyze_data_files, analyze_data_text
from apps.shell.agent.tools.registry import dispatch_tool_call
from apps.shell.agent.tools.terminal import (
    _TERMINAL_PROCESS_LOCK,
    _TERMINAL_PROCESSES,
    cancel_terminal_process_groups,
    run_terminal_command,
)
from apps.shell.agent.tools.workspace import (
    _apply_single_file_unified_diff,
    _atomic_write_text,
    _is_within,
    _safe_rel_path,
    _sha256_bytes,
    _sha256_file,
)
from packages.security import redact_sensitive_text

__all__ = [
    "ToolBroker",
    "_TERMINAL_PROCESSES",
    "_TERMINAL_PROCESS_LOCK",
    "cancel_terminal_process_groups",
]

_WORKSPACE_READ_MAX_BYTES = 200_000


def _redact_secrets(value: Any) -> str:
    return redact_sensitive_text(
        value,
        limit=0,
        collapse_whitespace=False,
        trim=False,
    )


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _with_native_postcondition_receipt(
    result: dict[str, Any],
    *,
    verified: bool,
) -> dict[str, Any]:
    """Expose an existing native read-back as a strict Runtime receipt."""

    if result.get("ok") is not True or not verified:
        return result
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {
        **result,
        "postcondition_verified": True,
        "data": {**data, "postcondition_verified": True},
    }


def _app_lifecycle_status_verified(
    result: dict[str, Any],
    *,
    expected_tool: str,
    expected_app_name: str,
    status_key: str,
    accepted_statuses: frozenset[str],
) -> bool:
    """Validate the exact app/status read-back before minting a receipt."""

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    observed_app_name = str(
        data.get("resolved_app_name")
        or data.get("app_name")
        or result.get("resolved_app_name")
        or result.get("app_name")
        or ""
    ).strip()
    observed_status = str(
        data.get(status_key) or result.get(status_key) or ""
    ).strip().casefold()
    return bool(
        result.get("ok") is True
        and str(result.get("action") or result.get("tool") or "").strip()
        == expected_tool
        and str(expected_app_name or "").strip()
        and observed_app_name
        and _app_names_match(expected_app_name, observed_app_name)
        and observed_status in accepted_statuses
    )


def _without_supplied_completion_claims(result: dict[str, Any]) -> dict[str, Any]:
    """Completion comes from native observations, not supplied flags."""

    authority_keys = {
        "postcondition_verified", "postcondition_ok", "verification_passed",
        "verified", "verified_observed_state", "verification_context_trusted",
        "verification_satisfied_by_native_receipt",
    }
    cleaned = {key: value for key, value in result.items() if key not in authority_keys}
    if isinstance(result.get("data"), dict):
        cleaned["data"] = {
            key: value for key, value in result["data"].items()
            if key not in authority_keys
        }
    return cleaned


def _native_media_result(result: dict[str, Any], expected_tool: str) -> bool:
    return bool(
        result.get("ok") is True
        and result.get("action") == expected_tool
        and not result.get("fallback_used")
        and not result.get("permission_error")
        and isinstance(result.get("data"), dict)
        and not result["data"].get("playback_state_unverified")
    )


def _apple_music_control_readback_verified(result: dict[str, Any], action: str) -> bool:
    if not _native_media_result(result, "media.apple_music_control"):
        return False
    data = result["data"]
    expected_action = desktop._clean_music_control_action(action)
    if data.get("control") != expected_action:
        return False
    state = str(data.get("player_state") or "").strip().casefold()
    if expected_action in {"play", "pause"}:
        return state == {"play": "playing", "pause": "paused"}[expected_action]
    if expected_action not in {"next", "previous"} or state not in {
        "playing", "paused", "stopped",
    }:
        return False
    before = str(data.get("before_track_id") or "").strip()
    after = str(data.get("after_track_id") or "").strip()
    source = data.get("track_identity_source")
    if source == "music_database_id":
        valid_ids = bool(
            re.fullmatch(r"[1-9][0-9]*", before)
            and re.fullmatch(r"[1-9][0-9]*", after)
        )
    elif source == "music_persistent_id":
        valid_ids = bool(
            re.fullmatch(r"[0-9A-Fa-f]{16}", before)
            and re.fullmatch(r"[0-9A-Fa-f]{16}", after)
            and before.strip("0") and after.strip("0")
        )
    else:
        valid_ids = False
    return bool(
        valid_ids and before.casefold() != after.casefold()
        and str(data.get("track_id") or "").strip() == after
        and data.get("track_identity_conflict") is not True
    )


def _apple_music_play_readback_verified(result: dict[str, Any], query: str) -> bool:
    if not _native_media_result(result, "media.apple_music_play"):
        return False
    data = result["data"]
    expected_query = str(query or "").strip()
    identity = data.get("album") if data.get("match_kind") == "album" else data.get("track")
    return bool(
        expected_query and str(data.get("query") or "").strip() == expected_query
        and data.get("status") == "played"
        and str(data.get("player_state") or "").strip().casefold() == "playing"
        and data.get("playback_started") is True
        and data.get("track_identity_verified") is True
        and desktop._apple_music_identity_matches(expected_query, identity)
        and str(data.get("track") or "").strip()
        and str(data.get("artist") or "").strip()
    )


def _apple_music_open_readback_verified(result: dict[str, Any]) -> bool:
    if not _native_media_result(result, "media.apple_music_open_and_play"):
        return False
    data = result["data"]
    return bool(
        data.get("app_name") == "Music" and data.get("open_ok") is True
        and data.get("playback_ok") is True and data.get("control") == "play"
        and str(data.get("player_state") or "").strip().casefold() == "playing"
    )


_WORKSPACE_LIST_FILE_TYPE_PATTERNS: dict[str, tuple[str, ...]] = {
    "screenshot": ("*.png", "*.jpg", "*.jpeg", "*.heic", "*.gif", "*.webp"),
    "image": ("*.png", "*.jpg", "*.jpeg", "*.heic", "*.gif", "*.webp"),
    "pdf": ("*.pdf",),
    "invoice": ("*invoice*", "*receipt*", "*发票*", "*票据*", "*收据*"),
    "document": ("*.doc", "*.docx", "*.pages", "*.rtf", "*.txt", "*.md"),
    "spreadsheet": ("*.csv", "*.tsv", "*.xls", "*.xlsx", "*.numbers"),
    "csv": ("*.csv",),
    "tsv": ("*.tsv",),
    "xlsx": ("*.xlsx",),
    "xls": ("*.xls",),
    "json": ("*.json",),
    "jsonl": ("*.jsonl",),
    "parquet": ("*.parquet",),
    "text_table": ("*.csv", "*.tsv", "*.xls", "*.xlsx", "*.json", "*.jsonl", "*.txt", "*.md", "*.markdown"),
    "archive": ("*.zip", "*.rar", "*.7z", "*.tar", "*.gz"),
    "audio": ("*.mp3", "*.wav", "*.aac", "*.m4a", "*.flac"),
    "video": ("*.mp4", "*.mov", "*.m4v", "*.avi", "*.mkv"),
}

_BROWSER_OWNED_TARGET_REQUIRED_TOOLS = {
    "browser.click",
    "browser.current_page",
    "browser.extract",
    "browser.extract_text",
    "browser.screenshot",
    "browser.type_text",
}

_FILE_ORGANIZE_GENERIC_DESTINATIONS = {
    "a folder",
    "folder",
    "directory",
    "dir",
    "one folder",
    "new folder",
    "一个",
    "一个文件夹",
    "新文件夹",
    "文件夹",
    "目录",
}
_FILE_ORGANIZE_TOP_LEVEL_DESTINATIONS = {
    "Desktop",
    "Documents",
    "Downloads",
    "Pictures",
    "Movies",
    "Music",
}
_FILE_ORGANIZE_DEFAULT_DESTINATIONS = {
    "invoice": "Invoices",
    "pdf": "PDFs",
    "screenshot": "Screenshots",
    "image": "Images",
    "document": "Documents",
    "spreadsheet": "Spreadsheets",
    "csv": "Spreadsheets",
    "tsv": "Spreadsheets",
    "xlsx": "Spreadsheets",
    "xls": "Spreadsheets",
    "archive": "Archives",
    "audio": "Audio",
    "video": "Video",
}


def _workspace_list_patterns(pattern: str, file_type: str) -> list[str]:
    patterns = _expand_workspace_list_pattern(pattern)
    if patterns:
        return patterns
    return list(_WORKSPACE_LIST_FILE_TYPE_PATTERNS.get(file_type.strip().casefold(), ()))


def _expand_workspace_list_pattern(pattern: str) -> list[str]:
    value = str(pattern or "").strip()
    if not value:
        return []
    expanded: list[str] = []
    for item in _split_workspace_list_patterns(value):
        item = item.strip()
        if not item:
            continue
        brace = re.fullmatch(r"(?P<prefix>.*)\{(?P<items>[^{}]+)\}(?P<suffix>.*)", item)
        if brace:
            prefix = brace.group("prefix")
            suffix = brace.group("suffix")
            expanded.extend(
                f"{prefix}{part.strip()}{suffix}"
                for part in brace.group("items").split(",")
                if part.strip()
            )
        else:
            expanded.append(item)
    return expanded


def _split_workspace_list_patterns(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char == "{":
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return parts


def _workspace_list_entry_matches(name: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    lowered = name.casefold()
    return any(fnmatch.fnmatchcase(lowered, pattern.casefold()) for pattern in patterns)


def _join_workspace_rel(*parts: str) -> str:
    clean_parts = [str(part or "").strip().strip("/\\") for part in parts if str(part or "").strip()]
    if not clean_parts:
        return "."
    return str(PurePosixPath(clean_parts[0]).joinpath(*clean_parts[1:]))


def _file_organize_clean_destination(value: str) -> str:
    destination = str(value or "").strip().strip("\"'`“”‘’")
    destination = re.sub(
        r"\s*(?:folder|directory|dir|文件夹|目录|中|里|内|下)\s*$",
        "",
        destination,
        flags=re.IGNORECASE,
    ).strip()
    if destination.casefold() in _FILE_ORGANIZE_GENERIC_DESTINATIONS:
        return ""
    return destination.rstrip("/\\。.,，；;")


def _file_organize_default_destination(file_type: str) -> str:
    return _FILE_ORGANIZE_DEFAULT_DESTINATIONS.get(file_type.strip().casefold(), "Organized Files")


def _file_organize_category_folder(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp"}:
        return "Images"
    if suffix == ".pdf":
        return "PDFs"
    if suffix in {".csv", ".tsv", ".xls", ".xlsx", ".numbers"}:
        return "Spreadsheets"
    if suffix in {".doc", ".docx", ".pages", ".rtf", ".txt", ".md", ".markdown"}:
        return "Documents"
    if suffix in {".zip", ".rar", ".7z", ".tar", ".gz"}:
        return "Archives"
    if suffix in {".mp3", ".wav", ".aac", ".m4a", ".flac"}:
        return "Audio"
    if suffix in {".mp4", ".mov", ".m4v", ".avi", ".mkv"}:
        return "Video"
    return "Other Files"


def _file_organize_unique_target(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = target.with_name(f"{stem} {index}{suffix}")
        if not candidate.exists():
            return candidate
    raise AgentRuntimeError("目标文件名冲突过多，已停止移动文件")


def _normalized_app_name(value: Any) -> str:
    name = str(value or "").strip()
    if name.casefold().endswith(".app"):
        name = name[:-4]
    return " ".join(name.casefold().split())


def _app_names_match(expected: Any, actual: Any) -> bool:
    normalized_expected = _normalized_app_name(expected)
    normalized_actual = _normalized_app_name(actual)
    return bool(
        normalized_expected
        and normalized_actual
        and normalized_expected == normalized_actual
    )


def _foreground_expected_app_name(
    fallback_app_name: str,
    step_results: Mapping[str, dict[str, Any]],
) -> str:
    for result in reversed(list(step_results.values())):
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        for key in ("resolved_app_name", "app_name"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    return str(fallback_app_name or "").strip()


def _call_foreground_bound_action(
    action: Any,
    *args: Any,
    expected_app_name: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Call a foreground action without weakening the broker's app binding.

    Current desktop actions accept ``expected_app_name`` and independently
    re-observe the foreground app.  Older injected adapters may not expose the
    keyword.  They are safe to call here only because ``_app_foreground_action``
    has just completed an exact, Runtime-owned active-window observation under
    the same foreground lock.  Unknown signatures keep the stricter call and
    therefore fail closed if they cannot honor the binding.
    """

    try:
        parameters = inspect.signature(action).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    supports_expected_app = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "expected_app_name"
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        )
        for parameter in parameters
    )
    if supports_expected_app:
        return action(
            *args,
            expected_app_name=expected_app_name,
            **kwargs,
        )
    if parameters:
        return action(*args, **kwargs)
    return action(
        *args,
        expected_app_name=expected_app_name,
        **kwargs,
    )


def _artifact_manifest_by_path(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, list):
        return {}
    manifest: dict[str, dict[str, str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        entry = {
            "path": path,
            "kind": str(item.get("kind") or "").strip(),
        }
        manifest.setdefault(path, entry)
    return manifest


def _data_analysis_artifact_metadata(
    artifact: dict[str, Any],
    *,
    source_kind: str,
    requested_outputs: list[str],
    manifest_by_path: dict[str, dict[str, str]],
    index: int,
) -> dict[str, Any]:
    path = str(artifact.get("path") or "").strip()
    manifest = manifest_by_path.get(path) or {}
    planned_kind = str(manifest.get("kind") or "").strip()
    metadata = dict(artifact)
    if source_kind:
        metadata["source_kind"] = source_kind
    if requested_outputs:
        metadata["requested_outputs"] = list(requested_outputs)
    if planned_kind:
        metadata["planned_kind"] = planned_kind
        metadata["manifest_index"] = index
    return metadata


def _data_analysis_artifact_manifest(artifacts: list[dict[str, Any]]) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    for artifact in artifacts:
        path = str(artifact.get("path") or "").strip()
        if not path:
            continue
        planned_kind = str(artifact.get("planned_kind") or "").strip()
        actual_kind = str(artifact.get("kind") or "").strip()
        entry = {"path": path, "kind": planned_kind or actual_kind}
        if planned_kind and actual_kind and planned_kind != actual_kind:
            entry["actual_kind"] = actual_kind
        manifest.append(entry)
    return manifest


def _requested_data_analysis_artifact_paths(
    artifact_path: str,
    artifact_paths: list[str] | None,
) -> list[str]:
    candidates = [str(artifact_path or "analysis-report.md").strip() or "analysis-report.md"]
    candidates.extend(str(path or "").strip() for path in artifact_paths or [])
    paths: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        rel = _safe_rel_path(candidate)
        if rel not in paths:
            paths.append(rel)
    return paths or ["analysis-report.md"]


@dataclass
class ToolBroker:
    """Controlled tools exposed to custom API agents."""

    workspace_policy: dict[str, Any]
    artifact_root: Path
    approvals: dict[str, bool] | None = None
    skills: list[dict[str, Any]] | None = None
    memory_store: Any | None = None
    future_task_store: Any | None = None
    foreground_lock: Any | None = None
    foreground_lock_owner: str = ""

    def __post_init__(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.approvals = self.approvals or {}
        self.skills = self.skills or []
        self._owned_browser_target_id = ""

    @property
    def workdir(self) -> Path:
        configured = str(self.workspace_policy.get("default_workdir") or "").strip()
        return Path(configured).expanduser() if configured else Path.cwd()

    def _scope_roots(self, key: str) -> list[Path]:
        scopes = self.workspace_policy.get(key) or []
        if isinstance(scopes, str):
            scopes = [scopes]
        roots = []
        for scope in scopes:
            rel = str(scope or ".").strip() or "."
            roots.append((self.workdir / rel).resolve())
        return roots or [self.workdir.resolve()]

    def _resolve_workspace_path(self, path: str, *, write: bool = False) -> Path:
        rel = _safe_rel_path(path or ".")
        target = (self.workdir / rel).resolve()
        key = "writable_scopes" if write else "readable_scopes"
        roots = self._scope_roots(key)
        if not any(_is_within(target, root) for root in roots):
            raise AgentWorkspaceBoundaryError("路径不在 Agent 允许的工作区范围内")
        return target

    def workspace_list(
        self,
        path: str = ".",
        *,
        pattern: str = "",
        file_type: str = "",
        include_metadata: bool = False,
    ) -> dict[str, Any]:
        target = self._resolve_workspace_path(path)
        display_path = path or "."
        if not target.exists():
            return {
                "ok": False,
                "path": display_path,
                "error": "路径不存在",
                "hint": "请先用 workspace.list 查看父目录，确认要访问的相对路径。",
            }
        if not target.is_dir():
            return {
                "ok": False,
                "path": display_path,
                "error": "workspace.list 只能列目录",
                "hint": "如果要读取文件内容，请改用 workspace.read。",
                "suggested_tool": "workspace.read",
            }
        patterns = _workspace_list_patterns(pattern, file_type)
        entries = []
        total_entries = 0
        for child in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            total_entries += 1
            if patterns and (
                child.is_dir() or not _workspace_list_entry_matches(child.name, patterns)
            ):
                continue
            if len(entries) >= 200:
                continue
            entry = {"name": child.name, "type": "dir" if child.is_dir() else "file"}
            if include_metadata:
                try:
                    stat = child.stat()
                except OSError:
                    stat = None
                if stat is not None:
                    entry.update(
                        {
                            "mtime": stat.st_mtime,
                            "mtime_ns": stat.st_mtime_ns,
                            "size": stat.st_size,
                        }
                    )
            entries.append(entry)
        result: dict[str, Any] = {"ok": True, "path": display_path, "entries": entries}
        if patterns:
            result["filter"] = {
                "pattern": pattern,
                "file_type": file_type,
                "expanded_patterns": patterns,
            }
            result["matched_count"] = len(entries)
            result["total_entries"] = total_entries
        return result

    def skill_read(self, skill_id: str = "", name: str = "") -> dict[str, Any]:
        wanted = str(skill_id or name or "").strip()
        if not wanted:
            return {
                "ok": False,
                "error": "skill.read 需要 skill_id 或 name",
                "available_skills": self._available_skill_refs(),
            }
        wanted_key = wanted.lower()
        for skill in self.skills or []:
            refs = {
                str(skill.get("skill_id") or "").strip().lower(),
                str(skill.get("name") or "").strip().lower(),
                str(skill.get("source_ref") or "").strip().lower(),
            }
            if wanted_key not in refs:
                continue
            markdown = _redact_secrets(str(skill.get("skill_markdown") or ""))
            return {
                "ok": True,
                "skill_id": str(skill.get("skill_id") or ""),
                "name": str(skill.get("name") or ""),
                "description": str(skill.get("description") or ""),
                "skill_markdown": markdown,
                "asset_paths": skill.get("asset_paths") or [],
            }
        return {
            "ok": False,
            "error": "Skill 未挂载到当前 Agent，不能读取完整手册",
            "requested": wanted,
            "available_skills": self._available_skill_refs(),
        }

    def memory_add(self, content: str, kind: str = "", scope: str = "") -> dict[str, Any]:
        if self.memory_store is None:
            return {"ok": False, "error": "当前运行未启用长期记忆存储"}
        return self.memory_store.add(content=content, kind=kind, scope=scope)

    def memory_replace(
        self,
        content: str,
        *,
        memory_id: str = "",
        old_content: str = "",
        kind: str = "",
        scope: str = "",
        approved: bool = False,
    ) -> dict[str, Any]:
        if self.memory_store is None:
            return {"ok": False, "error": "当前运行未启用长期记忆存储"}
        return self.memory_store.replace(
            memory_id=memory_id,
            old_content=old_content,
            content=content,
            kind=kind,
            scope=scope,
            approved=approved,
        )

    def memory_remove(
        self,
        *,
        memory_id: str = "",
        content: str = "",
        reason: str = "",
        approved: bool = False,
    ) -> dict[str, Any]:
        if self.memory_store is None:
            return {"ok": False, "error": "当前运行未启用长期记忆存储"}
        return self.memory_store.remove(
            memory_id=memory_id,
            content=content,
            reason=reason,
            approved=approved,
        )

    def future_task_schedule(
        self,
        *,
        title: str = "",
        prompt: str,
        delay_seconds: Any = None,
        scheduled_at_epoch: Any = None,
        cron: str = "",
        runnable_id: str = "",
        runnable_name: str = "",
    ) -> dict[str, Any]:
        if self.future_task_store is None:
            return {"ok": False, "error": "当前运行未启用 FutureTask 存储"}
        return self.future_task_store.schedule(
            title=title,
            prompt=prompt,
            runnable_id=runnable_id,
            runnable_name=runnable_name,
            delay_seconds=delay_seconds,
            scheduled_at_epoch=scheduled_at_epoch,
            cron=cron,
        )

    def future_task_list(
        self,
        *,
        include_finished: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        if self.future_task_store is None:
            return {"ok": False, "error": "当前运行未启用 FutureTask 存储"}
        return {
            "ok": True,
            "future_tasks": self.future_task_store.list_tasks(
                include_finished=include_finished,
                limit=limit,
            ),
        }

    def future_task_cancel(self, future_task_id: str, *, reason: str = "") -> dict[str, Any]:
        if self.future_task_store is None:
            return {"ok": False, "error": "当前运行未启用 FutureTask 存储"}
        try:
            return self.future_task_store.cancel(future_task_id, reason=reason)
        except KeyError:
            return {"ok": False, "error": "FutureTask 不存在", "future_task_id": future_task_id}

    def _available_skill_refs(self) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        for skill in self.skills or []:
            refs.append(
                {
                    "skill_id": str(skill.get("skill_id") or ""),
                    "name": str(skill.get("name") or ""),
                    "description": str(skill.get("description") or ""),
                }
            )
        return refs

    def workspace_read(self, path: str) -> dict[str, Any]:
        target = self._resolve_workspace_path(path)
        display_path = path or "."
        if not target.exists():
            return {
                "ok": False,
                "path": display_path,
                "error": "路径不存在",
                "hint": "请先用 workspace.list 查看父目录，确认要读取的文件相对路径。",
            }
        if target.is_dir():
            return {
                "ok": False,
                "path": display_path,
                "error": "workspace.read 只能读取文件",
                "hint": (
                    "这是一个目录；请改用 workspace.list 查看目录内容，"
                    "或选择目录中的具体文件再读取。"
                ),
                "suggested_tool": "workspace.list",
            }
        if not target.is_file():
            return {
                "ok": False,
                "path": display_path,
                "error": "workspace.read 只能读取文件",
                "hint": "请选择普通文本文件路径。",
            }
        # Read only the existing bounded prefix.  ``seek`` obtains the size
        # from the same open file handle without loading the rest of a large
        # file into memory, unlike ``Path.read_bytes()[:limit]``.
        with target.open("rb") as stream:
            stream.seek(0, 2)
            size_bytes = stream.tell()
            stream.seek(0)
            raw_content = stream.read(_WORKSPACE_READ_MAX_BYTES)
        try:
            content = raw_content.decode("utf-8", errors="strict")
            decoding_lossy = False
        except UnicodeDecodeError:
            # Preserve the historical replacement-character preview while
            # exposing that it is ineligible for exact-content verification.
            content = raw_content.decode("utf-8", errors="replace")
            decoding_lossy = True
        return {
            "ok": True,
            "path": display_path,
            "content": content,
            "truncated": size_bytes > len(raw_content),
            "size_bytes": size_bytes,
            "content_bytes": len(raw_content),
            "decoding_lossy": decoding_lossy,
        }

    def validate_tool_request(self, name: str, payload: dict[str, Any]) -> None:
        """Reject invalid workspace writes before presenting an approval.

        This preflight performs no tool effect. The execution adapter repeats
        its path checks after approval, including current symlink resolution.
        """
        if name == "workspace.write_patch":
            if str(payload.get("content") or "").strip():
                raise AgentRuntimeError(
                    "workspace.write_patch 不再支持 content 全量写入；"
                    "请提供单文件 unified diff patch"
                )
            self._resolve_workspace_path(str(payload.get("path") or ""), write=True)

    def workspace_write_patch(
        self,
        path: str,
        content: str = "",
        *,
        patch: str = "",
        expected_sha256: str = "",
        approved: bool = False,
    ) -> dict[str, Any]:
        if str(content or "").strip():
            raise AgentRuntimeError(
                "workspace.write_patch 不再支持 content 全量写入；"
                "请提供单文件 unified diff patch"
            )
        target = self._resolve_workspace_path(path, write=True)
        if not approved:
            return {"ok": False, "approval_required": True, "tool": "workspace.write_patch"}
        mode = "patch"
        if target.exists() and not target.is_file():
            return {"ok": False, "path": path, "error": "workspace.write_patch 只能写入普通文件"}
        if not target.exists():
            return {
                "ok": False,
                "path": path,
                "error": "workspace.write_patch patch 模式要求目标文件已存在",
            }
        before_bytes = target.read_bytes() if target.exists() else b""
        before_sha256 = _sha256_bytes(before_bytes)
        clean_expected_sha256 = str(expected_sha256 or "").strip()
        if clean_expected_sha256 and clean_expected_sha256 != before_sha256:
            return {
                "ok": False,
                "path": path,
                "error": "workspace.write_patch 当前文件 hash 与 expected_sha256 不匹配",
                "sha256_before": before_sha256,
            }
        try:
            before_text = before_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "path": path,
                "error": "workspace.write_patch patch 模式只支持 UTF-8 文本文件",
            }
        content = _apply_single_file_unified_diff(before_text, str(patch or ""), expected_path=path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, content)
        after_sha256 = _sha256_file(target)
        postcondition_verified = bool(
            target.is_file()
            and after_sha256 == _sha256_bytes(content.encode("utf-8"))
        )
        if not postcondition_verified:
            return {
                "ok": False,
                "path": path,
                "mode": mode,
                "sha256_before": before_sha256,
                "sha256_after": after_sha256,
                "verification_failed": True,
                "retryable": True,
                "error": "workspace_patch_readback_unverified",
            }
        return {
            "ok": True,
            "path": path,
            "mode": mode,
            "bytes": len(content.encode("utf-8")),
            "sha256_before": before_sha256,
            "sha256_after": after_sha256,
            "postcondition_verified": True,
        }

    def file_organize(
        self,
        path: str = ".",
        *,
        operation: str = "organize",
        file_type: str = "",
        pattern: str = "",
        destination: str = "",
        conflict_strategy: str = "keep_both",
        limit: int = 200,
        approved: bool = False,
    ) -> dict[str, Any]:
        clean_operation = str(operation or "organize").strip().casefold()
        if clean_operation not in {"organize", "archive", "move"}:
            return {
                "ok": False,
                "tool": "file.organize",
                "operation": clean_operation,
                "error": "file.organize 当前只支持 organize、archive 或 move，不执行删除或去重。",
            }
        clean_conflict_strategy = str(conflict_strategy or "keep_both").strip().casefold()
        if clean_conflict_strategy not in {"keep_both", "skip"}:
            return {
                "ok": False,
                "tool": "file.organize",
                "error": "conflict_strategy 只能是 keep_both 或 skip",
            }
        try:
            clean_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeError("file.organize 参数 limit 必须是 1-500 的整数") from exc
        if clean_limit < 1 or clean_limit > 500:
            raise AgentRuntimeError("file.organize 参数 limit 必须是 1-500 的整数")

        source_rel = _safe_rel_path(path or ".")
        source = self._resolve_workspace_path(source_rel, write=True)
        if not source.exists():
            return {"ok": False, "path": source_rel, "error": "路径不存在"}
        if not source.is_dir():
            return {"ok": False, "path": source_rel, "error": "file.organize 只能整理目录"}

        clean_file_type = str(file_type or "").strip()
        clean_pattern = str(pattern or "").strip()
        clean_destination = _file_organize_clean_destination(destination)
        input_preview = {
            "path": source_rel,
            "operation": clean_operation,
            "file_type": clean_file_type,
            "pattern": clean_pattern,
            "destination": clean_destination,
            "conflict_strategy": clean_conflict_strategy,
            "limit": clean_limit,
        }
        if not approved:
            return {
                "ok": False,
                "approval_required": True,
                "tool": "file.organize",
                "input_preview": input_preview,
            }

        patterns = _workspace_list_patterns(clean_pattern, clean_file_type)
        moved: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        matched = 0
        for child in sorted(source.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir():
                continue
            if child.is_symlink() or not child.is_file():
                skipped.append(
                    {
                        "path": self._workspace_display_path(child),
                        "reason": "unsupported_file_type",
                    }
                )
                continue
            if patterns and not _workspace_list_entry_matches(child.name, patterns):
                continue
            matched += 1
            if matched > clean_limit:
                skipped.append(
                    {
                        "path": self._workspace_display_path(child),
                        "reason": "limit_exceeded",
                    }
                )
                continue
            destination_rel = self._file_organize_destination_rel(
                source_rel=source_rel,
                source_child=child,
                destination=clean_destination,
                file_type=clean_file_type,
            )
            destination_dir = self._resolve_workspace_path(destination_rel, write=True)
            destination_dir.mkdir(parents=True, exist_ok=True)
            target = destination_dir / child.name
            if target.resolve() == child.resolve():
                skipped.append(
                    {
                        "path": self._workspace_display_path(child),
                        "reason": "already_in_destination",
                    }
                )
                continue
            if target.exists() and clean_conflict_strategy == "skip":
                skipped.append(
                    {
                        "path": self._workspace_display_path(child),
                        "reason": "target_exists",
                    }
                )
                continue
            final_target = _file_organize_unique_target(target)
            child.rename(final_target)
            moved.append(
                {
                    "from": self._workspace_display_path(child),
                    "to": self._workspace_display_path(final_target),
                }
            )
        return {
            "ok": True,
            "tool": "file.organize",
            "operation": clean_operation,
            "path": source_rel,
            "file_type": clean_file_type,
            "pattern": clean_pattern,
            "destination": clean_destination,
            "matched_count": matched,
            "moved_count": len(moved),
            "skipped_count": len(skipped),
            "moved": moved,
            "skipped": skipped,
            "summary": f"Moved {len(moved)} file(s) from {source_rel}.",
        }

    def _file_organize_destination_rel(
        self,
        *,
        source_rel: str,
        source_child: Path,
        destination: str,
        file_type: str,
    ) -> str:
        if destination:
            if "/" in destination or destination in _FILE_ORGANIZE_TOP_LEVEL_DESTINATIONS:
                return _safe_rel_path(destination)
            return _safe_rel_path(_join_workspace_rel(source_rel, destination))
        folder = _file_organize_default_destination(file_type)
        if not file_type:
            folder = _file_organize_category_folder(source_child)
        return _safe_rel_path(_join_workspace_rel(source_rel, folder))

    def _workspace_display_path(self, target: Path) -> str:
        try:
            rel = target.resolve().relative_to(self.workdir.resolve())
        except ValueError:
            return str(target)
        text = rel.as_posix()
        return text or "."

    def terminal_run(
        self,
        command: str,
        *,
        approved: bool = False,
        timeout_seconds: int = 30,
        shell: bool = False,
    ) -> dict[str, Any]:
        if not approved:
            return {
                "ok": False,
                "approval_required": True,
                "tool": "terminal.run",
                "input_preview": {"command": command, "shell": bool(shell)},
            }
        return run_terminal_command(
            command,
            workdir=self.workdir,
            timeout_seconds=timeout_seconds,
            shell=shell,
        )

    def artifact_write(self, path: str, content: str) -> dict[str, Any]:
        rel = _safe_rel_path(path)
        target = (self.artifact_root / rel).resolve()
        if not _is_within(target, self.artifact_root):
            raise AgentRuntimeError("artifact 路径越界")
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_content = _redact_secrets(content)
        target.write_text(safe_content, encoding="utf-8")
        expected = safe_content.encode("utf-8")
        postcondition_verified = bool(target.is_file() and target.read_bytes() == expected)
        if not postcondition_verified:
            return {
                "ok": False,
                "path": rel,
                "verification_failed": True,
                "retryable": True,
                "error": "artifact_write_readback_unverified",
            }
        return {
            "ok": True,
            "path": rel,
            "bytes": len(expected),
            "postcondition_verified": True,
        }

    def artifact_write_bytes(self, path: str, content: bytes) -> dict[str, Any]:
        rel = _safe_rel_path(path)
        target = (self.artifact_root / rel).resolve()
        if not _is_within(target, self.artifact_root):
            raise AgentRuntimeError("artifact 路径越界")
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = bytes(content or b"")
        target.write_bytes(expected)
        postcondition_verified = bool(target.is_file() and target.read_bytes() == expected)
        if not postcondition_verified:
            return {
                "ok": False,
                "path": rel,
                "verification_failed": True,
                "retryable": True,
                "error": "artifact_write_readback_unverified",
            }
        return {
            "ok": True,
            "path": rel,
            "bytes": target.stat().st_size,
            "postcondition_verified": True,
        }

    def data_analyze(
        self,
        path: str,
        *,
        paths: list[str] | None = None,
        content: str = "",
        display_path: str = "",
        artifact_path: str = "analysis-report.md",
        artifact_paths: list[str] | None = None,
        max_rows: int = 1000,
        source_kind: str = "",
        requested_outputs: list[str] | None = None,
        artifact_manifest: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        clean_content = str(content or "")
        display_source = str(display_path or path or "captured-data").strip() or "captured-data"
        if clean_content.strip():
            result = analyze_data_text(
                clean_content,
                display_path=display_source,
                artifact_path=artifact_path or "analysis-report.md",
                artifact_paths=artifact_paths,
                max_rows=max_rows,
                source_kind=source_kind or "text_table",
            )
        elif paths:
            clean_paths = [str(item or "").strip() for item in paths if str(item or "").strip()]
            if not clean_paths:
                return {
                    "ok": False,
                    "path": display_source,
                    "error": "未提供可分析的数据文件",
                    "hint": "请先用 workspace.list 查看目录，确认要分析的文件相对路径。",
                }
            resolved_paths: list[Path] = []
            for item in clean_paths:
                target = self._resolve_workspace_path(item)
                if not target.exists():
                    return {
                        "ok": False,
                        "path": item,
                        "paths": clean_paths,
                        "error": "路径不存在",
                        "hint": "请先用 workspace.list 查看父目录，确认要分析的文件相对路径。",
                    }
                if target.is_dir():
                    return {
                        "ok": False,
                        "path": item,
                        "paths": clean_paths,
                        "error": "data.analyze 只能分析文件",
                        "hint": "这是一个目录；请先用 workspace.list 选择目录中的数据文件。",
                        "suggested_tool": "workspace.list",
                    }
                resolved_paths.append(target)
            result = analyze_data_files(
                resolved_paths,
                display_paths=clean_paths,
                artifact_path=artifact_path or "analysis-report.md",
                artifact_paths=artifact_paths,
                max_rows=max_rows,
                source_kind=source_kind,
            )
            if not result.get("ok"):
                return result
        else:
            target = self._resolve_workspace_path(path)
            display_source = path or "."
            if not target.exists():
                return {
                    "ok": False,
                    "path": display_source,
                    "error": "路径不存在",
                    "hint": "请先用 workspace.list 查看父目录，确认要分析的文件相对路径。",
                }
            if target.is_dir():
                return {
                    "ok": False,
                    "path": display_source,
                    "error": "data.analyze 只能分析文件",
                    "hint": "这是一个目录；请先用 workspace.list 选择目录中的数据文件。",
                    "suggested_tool": "workspace.list",
                }
            result = analyze_data_file(
                target,
                display_path=display_source,
                artifact_path=artifact_path or "analysis-report.md",
                artifact_paths=artifact_paths,
                max_rows=max_rows,
            )
            if not result.get("ok"):
                return result
        if not result.get("ok"):
            if source_kind and not result.get("source_kind"):
                return {
                    **result,
                    "source_kind": source_kind,
                }
            return result
        analysis_source_kind = str(result.get("source_kind") or source_kind or "").strip()
        clean_requested_outputs = _clean_string_list(requested_outputs)
        manifest_by_path = _artifact_manifest_by_path(artifact_manifest)
        artifact = self.artifact_write(
            str(result.get("artifact_path") or "analysis-report.md"),
            str(result.get("artifact_content") or ""),
        )
        artifacts = [
            _data_analysis_artifact_metadata(
                {
                    "path": artifact["path"],
                    "kind": "markdown",
                    "mime_type": "text/markdown",
                    "size_bytes": artifact["bytes"],
                },
                source_kind=analysis_source_kind,
                requested_outputs=clean_requested_outputs,
                manifest_by_path=manifest_by_path,
                index=0,
            )
        ]
        for extra_index, extra in enumerate(result.get("extra_artifacts") or [], start=1):
            if not isinstance(extra, dict):
                continue
            extra_path = str(extra.get("path") or "").strip()
            if not extra_path:
                continue
            if extra.get("content_bytes") is not None:
                written = self.artifact_write_bytes(extra_path, bytes(extra.get("content_bytes") or b""))
            else:
                written = self.artifact_write(extra_path, str(extra.get("content") or ""))
            artifacts.append(
                _data_analysis_artifact_metadata(
                    {
                        "path": written["path"],
                        "kind": str(extra.get("kind") or "artifact"),
                        "mime_type": str(extra.get("mime_type") or ""),
                        "size_bytes": written["bytes"],
                        **(
                            {"width": extra.get("width")}
                            if extra.get("width") is not None
                            else {}
                        ),
                        **(
                            {"height": extra.get("height")}
                            if extra.get("height") is not None
                            else {}
                        ),
                    },
                    source_kind=analysis_source_kind,
                    requested_outputs=clean_requested_outputs,
                    manifest_by_path=manifest_by_path,
                    index=extra_index,
                )
            )
        expected_artifact_paths = _requested_data_analysis_artifact_paths(
            artifact_path,
            artifact_paths,
        )
        postcondition_verified = self._data_analysis_artifacts_stat_verified(
            artifacts,
            expected_paths=expected_artifact_paths,
        )
        response = {
            **{
                key: value
                for key, value in result.items()
                if key not in {"artifact_content", "extra_artifacts"}
            },
            "artifact_manifest": _data_analysis_artifact_manifest(artifacts),
            "artifact": artifacts[0],
            "artifacts": artifacts,
            "postcondition_verified": postcondition_verified,
        }
        if postcondition_verified:
            return response
        return {
            **response,
            "ok": False,
            "verification_failed": True,
            "retryable": True,
            "error": "data_analysis_artifact_unverified",
            "hint": "The requested analysis artifacts were not verified at their exact paths.",
        }

    def _data_analysis_artifacts_stat_verified(
        self,
        artifacts: list[dict[str, Any]],
        *,
        expected_paths: list[str],
    ) -> bool:
        observed_paths: list[str] = []
        for artifact in artifacts:
            try:
                rel = _safe_rel_path(str(artifact.get("path") or ""))
            except AgentRuntimeError:
                return False
            target = (self.artifact_root / rel).resolve()
            if not _is_within(target, self.artifact_root):
                return False
            try:
                stat = target.stat()
            except OSError:
                return False
            if not target.is_file():
                return False
            size_bytes = artifact.get("size_bytes")
            if (
                not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes < 0
                or stat.st_size != size_bytes
            ):
                return False
            observed_paths.append(rel)
        return bool(observed_paths and observed_paths == expected_paths)

    def screen_capture(self, *, reason: str = "") -> dict[str, Any]:
        rel = Path("screenshots") / "current-screen.png"
        target = (self.artifact_root / rel).resolve()
        if not _is_within(target, self.artifact_root):
            raise AgentRuntimeError("screen artifact 路径越界")
        target.parent.mkdir(parents=True, exist_ok=True)
        result = desktop.screen_capture(target)
        if not result.get("ok"):
            return result
        data = dict(result.get("data") or {})
        data["path"] = str(rel)
        return {
            **result,
            "summary": result.get("summary") or "Captured current screen",
            "reason": str(reason or "").strip(),
            "artifact": {
                "path": str(rel),
                "kind": "image",
                "mime_type": data.get("mime_type") or "image/png",
                "size_bytes": data.get("size") or data.get("size_bytes"),
                "width": data.get("width"),
                "height": data.get("height"),
            },
            "data": data,
        }

    def desktop_active_window(self) -> dict[str, Any]:
        return desktop.active_window()

    def desktop_permissions(self) -> dict[str, Any]:
        return desktop.permissions()

    def desktop_permissions_verify(self) -> dict[str, Any]:
        return {
            **desktop.permissions(active_verification=True),
            "action": "desktop.permissions.verify",
        }

    def desktop_permission_preflight(self) -> dict[str, Any]:
        return desktop.permission_preflight()

    def desktop_running_apps(self) -> dict[str, Any]:
        return desktop.running_apps()

    def desktop_list_apps(self, query: str = "", limit: Any = 200) -> dict[str, Any]:
        return desktop.list_apps(query=query, limit=limit)

    def desktop_windows(self, app_name: str = "") -> dict[str, Any]:
        return desktop.windows(app_name)

    def desktop_ui_elements(
        self,
        role_filter: str = "",
        limit: Any = 80,
        app_name: str = "",
    ) -> dict[str, Any]:
        return desktop.ui_elements(
            role_filter=role_filter,
            limit=limit,
            app_name=app_name,
        )

    def desktop_inspect_app(
        self,
        app_name: str,
        *,
        open_if_needed: Any = False,
        focus: Any = False,
        role_filter: str = "",
        limit: Any = 80,
    ) -> dict[str, Any]:
        return desktop.inspect_app(
            app_name,
            open_if_needed=open_if_needed,
            focus=focus,
            role_filter=role_filter,
            limit=limit,
        )

    def desktop_click_ui_element(
        self,
        target: str,
        *,
        role_filter: str = "",
        limit: Any = 80,
        click_count: Any = 1,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.click_ui_element",
            lambda: desktop.click_ui_element(
                target,
                role_filter=role_filter,
                limit=limit,
                click_count=click_count,
            ),
        )

    def desktop_type_into_ui_element(
        self,
        target: str,
        text: str,
        *,
        role_filter: str = "",
        limit: Any = 80,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.type_into_ui_element",
            lambda: desktop.type_into_ui_element(
                target,
                text,
                role_filter=role_filter,
                limit=limit,
            ),
        )

    def app_status(self, app_name: str) -> dict[str, Any]:
        return desktop.app_status(app_name)

    def app_open(self, app_name: str) -> dict[str, Any]:
        result = desktop.app_open(app_name)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return _with_native_postcondition_receipt(
            result,
            verified=data.get("launch_verified") is True,
        )

    def app_focus(self, app_name: str) -> dict[str, Any]:
        result = desktop.app_focus(app_name)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return _with_native_postcondition_receipt(
            result,
            verified=data.get("focus_verified") is True,
        )

    def app_focus_window(self, app_name: str, title_contains: str) -> dict[str, Any]:
        result = desktop.app_focus_window(app_name, title_contains)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        expected_title = str(title_contains or "").strip().casefold()
        observed_title = str(data.get("window_title") or "").strip().casefold()
        return _with_native_postcondition_receipt(
            result,
            verified=bool(
                str(data.get("focus_status") or "").strip() == "focused"
                and expected_title
                and expected_title in observed_title
            ),
        )

    def app_open_and_safe_type_text(self, app_name: str, text: str) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.open_and_safe_type_text",
            lambda: self._app_foreground_action(
                "app.open_and_safe_type_text",
                app_name,
                setup_steps=(
                    ("open", lambda: desktop.app_open(app_name)),
                    ("focus", lambda: desktop.app_focus(app_name)),
                ),
                action_step=("safe_type_text", lambda: desktop.desktop_safe_type_text(text)),
            ),
        )

    def app_focus_and_safe_type_text(self, app_name: str, text: str) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.focus_and_safe_type_text",
            lambda: self._app_foreground_action(
                "app.focus_and_safe_type_text",
                app_name,
                setup_steps=(("focus", lambda: desktop.app_focus(app_name)),),
                action_step=("safe_type_text", lambda: desktop.desktop_safe_type_text(text)),
            ),
        )

    def app_open_and_safe_shortcut(self, app_name: str, action: str) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.open_and_safe_shortcut",
            lambda: self._app_foreground_action(
                "app.open_and_safe_shortcut",
                app_name,
                setup_steps=(
                    ("open", lambda: desktop.app_open(app_name)),
                    ("focus", lambda: desktop.app_focus(app_name)),
                ),
                action_step=("safe_shortcut", lambda: desktop.desktop_safe_shortcut(action)),
            ),
        )

    def app_focus_and_safe_shortcut(self, app_name: str, action: str) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.focus_and_safe_shortcut",
            lambda: self._app_foreground_action(
                "app.focus_and_safe_shortcut",
                app_name,
                setup_steps=(("focus", lambda: desktop.app_focus(app_name)),),
                action_step=("safe_shortcut", lambda: desktop.desktop_safe_shortcut(action)),
            ),
        )

    def app_open_and_safe_key(
        self,
        app_name: str,
        action: str,
        *,
        repeat_count: Any = 1,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.open_and_safe_key",
            lambda: self._app_foreground_action(
                "app.open_and_safe_key",
                app_name,
                setup_steps=(
                    ("open", lambda: desktop.app_open(app_name)),
                    ("focus", lambda: desktop.app_focus(app_name)),
                ),
                action_step=(
                    "safe_key",
                    lambda: desktop.desktop_safe_key(action, repeat_count=repeat_count),
                ),
            ),
        )

    def app_focus_and_safe_key(
        self,
        app_name: str,
        action: str,
        *,
        repeat_count: Any = 1,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.focus_and_safe_key",
            lambda: self._app_foreground_action(
                "app.focus_and_safe_key",
                app_name,
                setup_steps=(("focus", lambda: desktop.app_focus(app_name)),),
                action_step=(
                    "safe_key",
                    lambda: desktop.desktop_safe_key(action, repeat_count=repeat_count),
                ),
            ),
        )

    def app_open_and_hotkey(
        self,
        app_name: str,
        key: str,
        *,
        modifiers: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.open_and_hotkey",
            lambda: self._app_foreground_action(
                "app.open_and_hotkey",
                app_name,
                setup_steps=(
                    ("open", lambda: desktop.app_open(app_name)),
                    ("focus", lambda: desktop.app_focus(app_name)),
                ),
                action_step=(
                    "hotkey",
                    lambda: desktop.desktop_hotkey(key, modifiers=modifiers),
                ),
            ),
        )

    def app_focus_and_hotkey(
        self,
        app_name: str,
        key: str,
        *,
        modifiers: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.focus_and_hotkey",
            lambda: self._app_foreground_action(
                "app.focus_and_hotkey",
                app_name,
                setup_steps=(("focus", lambda: desktop.app_focus(app_name)),),
                action_step=(
                    "hotkey",
                    lambda: desktop.desktop_hotkey(key, modifiers=modifiers),
                ),
            ),
        )

    def app_open_and_safe_scroll(
        self,
        app_name: str,
        direction: str,
        *,
        pages: Any = 1,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.open_and_safe_scroll",
            lambda: self._app_foreground_action(
                "app.open_and_safe_scroll",
                app_name,
                setup_steps=(
                    ("open", lambda: desktop.app_open(app_name)),
                    ("focus", lambda: desktop.app_focus(app_name)),
                ),
                action_step=(
                    "safe_scroll",
                    lambda: desktop.desktop_safe_scroll(direction, pages=pages),
                ),
            ),
        )

    def app_focus_and_safe_scroll(
        self,
        app_name: str,
        direction: str,
        *,
        pages: Any = 1,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.focus_and_safe_scroll",
            lambda: self._app_foreground_action(
                "app.focus_and_safe_scroll",
                app_name,
                setup_steps=(("focus", lambda: desktop.app_focus(app_name)),),
                action_step=(
                    "safe_scroll",
                    lambda: desktop.desktop_safe_scroll(direction, pages=pages),
                ),
            ),
        )

    def app_open_and_safe_click(
        self,
        app_name: str,
        x: Any,
        y: Any,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.open_and_safe_click",
            lambda: self._app_foreground_action(
                "app.open_and_safe_click",
                app_name,
                setup_steps=(
                    ("open", lambda: desktop.app_open(app_name)),
                    ("focus", lambda: desktop.app_focus(app_name)),
                ),
                action_step=(
                    "safe_click",
                    lambda: desktop.desktop_safe_click(x, y),
                ),
            ),
        )

    def app_focus_and_safe_click(
        self,
        app_name: str,
        x: Any,
        y: Any,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.focus_and_safe_click",
            lambda: self._app_foreground_action(
                "app.focus_and_safe_click",
                app_name,
                setup_steps=(("focus", lambda: desktop.app_focus(app_name)),),
                action_step=(
                    "safe_click",
                    lambda: desktop.desktop_safe_click(x, y),
                ),
            ),
        )

    def app_open_and_click_ui_element(
        self,
        app_name: str,
        target: str,
        *,
        role_filter: str = "",
        limit: Any = 80,
        click_count: Any = 1,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.open_and_click_ui_element",
            lambda: self._app_foreground_action(
                "app.open_and_click_ui_element",
                app_name,
                setup_steps=(
                    ("open", lambda: desktop.app_open(app_name)),
                    ("focus", lambda: desktop.app_focus(app_name)),
                ),
                action_step=(
                    "click_ui_element",
                    lambda expected_app_name: _call_foreground_bound_action(
                        desktop.click_ui_element,
                        target,
                        role_filter=role_filter,
                        limit=limit,
                        click_count=click_count,
                        expected_app_name=expected_app_name,
                    ),
                    True,
                ),
            ),
        )

    def app_focus_and_click_ui_element(
        self,
        app_name: str,
        target: str,
        *,
        role_filter: str = "",
        limit: Any = 80,
        click_count: Any = 1,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.focus_and_click_ui_element",
            lambda: self._app_foreground_action(
                "app.focus_and_click_ui_element",
                app_name,
                setup_steps=(("focus", lambda: desktop.app_focus(app_name)),),
                action_step=(
                    "click_ui_element",
                    lambda expected_app_name: _call_foreground_bound_action(
                        desktop.click_ui_element,
                        target,
                        role_filter=role_filter,
                        limit=limit,
                        click_count=click_count,
                        expected_app_name=expected_app_name,
                    ),
                    True,
                ),
            ),
        )

    def app_open_and_type_into_ui_element(
        self,
        app_name: str,
        target: str,
        text: str,
        *,
        role_filter: str = "",
        limit: Any = 80,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.open_and_type_into_ui_element",
            lambda: self._app_foreground_action(
                "app.open_and_type_into_ui_element",
                app_name,
                setup_steps=(
                    ("open", lambda: desktop.app_open(app_name)),
                    ("focus", lambda: desktop.app_focus(app_name)),
                ),
                action_step=(
                    "type_into_ui_element",
                    lambda expected_app_name: _call_foreground_bound_action(
                        desktop.type_into_ui_element,
                        target,
                        text,
                        role_filter=role_filter,
                        limit=limit,
                        expected_app_name=expected_app_name,
                    ),
                    True,
                ),
            ),
        )

    def app_focus_and_type_into_ui_element(
        self,
        app_name: str,
        target: str,
        text: str,
        *,
        role_filter: str = "",
        limit: Any = 80,
    ) -> dict[str, Any]:
        return self._with_foreground_lock(
            "app.focus_and_type_into_ui_element",
            lambda: self._app_foreground_action(
                "app.focus_and_type_into_ui_element",
                app_name,
                setup_steps=(("focus", lambda: desktop.app_focus(app_name)),),
                action_step=(
                    "type_into_ui_element",
                    lambda expected_app_name: _call_foreground_bound_action(
                        desktop.type_into_ui_element,
                        target,
                        text,
                        role_filter=role_filter,
                        limit=limit,
                        expected_app_name=expected_app_name,
                    ),
                    True,
                ),
            ),
        )

    def app_show(self, app_name: str) -> dict[str, Any]:
        result = desktop.app_show(app_name)
        return _with_native_postcondition_receipt(
            result,
            verified=_app_lifecycle_status_verified(
                result,
                expected_tool="app.show",
                expected_app_name=app_name,
                status_key="show_status",
                accepted_statuses=frozenset({"launched", "shown"}),
            ),
        )

    def app_hide(self, app_name: str) -> dict[str, Any]:
        result = desktop.app_hide(app_name)
        return _with_native_postcondition_receipt(
            result,
            verified=_app_lifecycle_status_verified(
                result,
                expected_tool="app.hide",
                expected_app_name=app_name,
                status_key="hide_status",
                accepted_statuses=frozenset({"hidden"}),
            ),
        )

    def app_minimize(self, app_name: str) -> dict[str, Any]:
        result = desktop.app_minimize(app_name)
        return _with_native_postcondition_receipt(
            result,
            verified=_app_lifecycle_status_verified(
                result,
                expected_tool="app.minimize",
                expected_app_name=app_name,
                status_key="minimize_status",
                accepted_statuses=frozenset({"minimized"}),
            ),
        )

    def app_quit(self, app_name: str) -> dict[str, Any]:
        result = _without_supplied_completion_claims(desktop.app_quit(app_name))
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return _with_native_postcondition_receipt(
            result,
            verified=bool(
                result.get("ok") is True and result.get("action") == "app.quit"
                and not result.get("permission_error") and not result.get("fallback_used")
                and _app_names_match(app_name, str(data.get("app_name") or ""))
                and data.get("quit_status") in {"quit", "not_running"}
                and data.get("launch_status") == "not_running"
                and data.get("running") is False and data.get("quit_verified") is True
            ),
        )

    def desktop_reveal_path(self, path: str) -> dict[str, Any]:
        return desktop.reveal_path(path)

    def desktop_open_path(self, path: str) -> dict[str, Any]:
        return desktop.open_path(path)

    def desktop_open_path_with_app(self, path: str, app_name: str) -> dict[str, Any]:
        return desktop.open_path_with_app(path, app_name)

    def media_apple_music_play(self, query: str) -> dict[str, Any]:
        result = _without_supplied_completion_claims(desktop.apple_music_play(query))
        return _with_native_postcondition_receipt(
            result, verified=_apple_music_play_readback_verified(result, query)
        )

    def media_apple_music_status(self) -> dict[str, Any]:
        return desktop.apple_music_status()

    def media_apple_music_open_and_play(self) -> dict[str, Any]:
        result = _without_supplied_completion_claims(desktop.apple_music_open_and_play())
        return _with_native_postcondition_receipt(
            result, verified=_apple_music_open_readback_verified(result)
        )

    def media_apple_music_control(self, action: str) -> dict[str, Any]:
        result = _without_supplied_completion_claims(desktop.apple_music_control(action))
        return _with_native_postcondition_receipt(
            result, verified=_apple_music_control_readback_verified(result, action)
        )

    def media_music_app_open_and_play(self, app_name: str) -> dict[str, Any]:
        result = _without_supplied_completion_claims(desktop.music_app_open_and_play(app_name))
        return _with_native_postcondition_receipt(
            result, verified=str(app_name or "").strip() == "Music"
            and _apple_music_open_readback_verified(result),
        )

    def media_music_app_control(self, app_name: str, action: str) -> dict[str, Any]:
        return desktop.music_app_control(app_name, action)

    def media_system_control(self, action: str) -> dict[str, Any]:
        return desktop.system_media_control(action)

    def system_settings_open(self, target: str) -> dict[str, Any]:
        return desktop.system_settings_open(target)

    def system_volume(
        self,
        action: str,
        *,
        level: Any = None,
        step: Any = None,
    ) -> dict[str, Any]:
        return desktop.system_volume(action, level=level, step=step)

    def system_brightness(
        self,
        action: str,
        *,
        step: Any = None,
    ) -> dict[str, Any]:
        return desktop.system_brightness(action, step=step)

    def system_display_sleep(self) -> dict[str, Any]:
        return desktop.system_display_sleep()

    def system_screen_saver_start(self) -> dict[str, Any]:
        return desktop.system_screen_saver_start()

    def clipboard_write(self, text: str) -> dict[str, Any]:
        return desktop.clipboard_write(text)

    def clipboard_read(self, *, max_chars: Any = 2000) -> dict[str, Any]:
        return desktop.clipboard_read(max_chars=max_chars)

    def notes_create(
        self,
        body: str,
        *,
        title: str = "",
        folder_name: str = "",
    ) -> dict[str, Any]:
        return desktop.notes_create(body, title=title, folder_name=folder_name)

    def reminders_create(
        self,
        title: str,
        *,
        due_at: Any = None,
        list_name: str = "",
    ) -> dict[str, Any]:
        return desktop.reminders_create(title, due_at=due_at, list_name=list_name)

    def calendar_create_event(
        self,
        title: str,
        *,
        start_at: Any,
        end_at: Any = None,
        calendar_name: str = "",
    ) -> dict[str, Any]:
        return desktop.calendar_create_event(
            title,
            start_at=start_at,
            end_at=end_at,
            calendar_name=calendar_name,
        )

    def desktop_safe_shortcut(self, action: str) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.safe_shortcut",
            lambda: desktop.desktop_safe_shortcut(action),
        )

    def desktop_safe_key(self, action: str, *, repeat_count: Any = 1) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.safe_key",
            lambda: desktop.desktop_safe_key(action, repeat_count=repeat_count),
        )

    def desktop_safe_type_text(self, text: str) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.safe_type_text",
            lambda: desktop.desktop_safe_type_text(text),
        )

    def desktop_search_submit(self) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.search_submit",
            desktop.desktop_search_submit,
        )

    def desktop_hide_app(self) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.hide_app",
            desktop.desktop_hide_app,
        )

    def desktop_show_all_apps(self) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.show_all_apps",
            desktop.desktop_show_all_apps,
        )

    def desktop_minimize_window(self) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.minimize_window",
            desktop.desktop_minimize_window,
        )

    def desktop_close_window(self) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.close_window",
            desktop.desktop_close_window,
        )

    def desktop_quit_app(self) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.quit_app",
            desktop.desktop_quit_app,
        )

    def desktop_hotkey(self, key: str, *, modifiers: list[str] | None = None) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.hotkey",
            lambda: desktop.desktop_hotkey(key, modifiers=modifiers),
        )

    def desktop_submit_foreground(self, action: str = "submit") -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.submit_foreground",
            lambda: desktop.desktop_submit_foreground(action),
        )

    def runtime_exact_submit_foreground(
        self,
        action: str,
        *,
        validate_pre: Any,
        observe_post: Any,
    ) -> dict[str, Any]:
        """Atomically revalidate a Runtime-bound draft and dispatch Return.

        The callbacks close over process-private Runtime authority.  Neither
        the expected text nor its target identity is serialized through the
        public tool payload or trusted from provider output.
        """

        def submit_under_one_foreground_lock() -> dict[str, Any]:
            pre_snapshot = desktop.ui_elements()
            if not bool(validate_pre(pre_snapshot)):
                return {
                    "ok": False,
                    "action": "desktop.submit_foreground",
                    "status": "blocked",
                    "reason": "prepared_submit_target_revalidation_failed",
                    "error": "prepared_submit_target_revalidation_failed",
                    "summary": (
                        "Submit was not dispatched because the prepared app, "
                        "window, editable target, or exact content changed."
                    ),
                    "retryable": False,
                }
            dispatched = desktop.desktop_submit_foreground(action)
            if dispatched.get("ok") is not True:
                return dispatched
            observe_post(desktop.ui_elements())
            return dispatched

        return self._with_foreground_lock(
            "desktop.submit_foreground",
            submit_under_one_foreground_lock,
        )

    def desktop_type_text(self, text: str) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.type_text",
            lambda: desktop.desktop_type_text(text),
        )

    def desktop_safe_click(self, x: Any, y: Any) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.safe_click",
            lambda: desktop.desktop_safe_click(x, y),
        )

    def desktop_safe_scroll(self, direction: str, *, pages: Any = 1) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.safe_scroll",
            lambda: desktop.desktop_safe_scroll(direction, pages=pages),
        )

    def desktop_click(self, x: Any, y: Any, *, click_count: Any = 1) -> dict[str, Any]:
        return self._with_foreground_lock(
            "desktop.click",
            lambda: desktop.desktop_click(x, y, click_count=click_count),
        )

    def _app_foreground_action(
        self,
        tool_name: str,
        app_name: str,
        *,
        setup_steps: tuple[tuple[str, Any], ...],
        action_step: tuple[Any, ...],
    ) -> dict[str, Any]:
        clean_app_name = str(app_name or "").strip()
        step_results: dict[str, dict[str, Any]] = {}
        fallback_used = False
        for step_name, step in setup_steps:
            result = step()
            step_results[step_name] = result
            fallback_used = fallback_used or bool(result.get("fallback_used"))
            if _foreground_focus_not_verified(step_name, result):
                result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
                data = dict(result_data)
                if clean_app_name:
                    data["app_name"] = clean_app_name
                condition = _foreground_blocking_condition(result)
                data.setdefault("blocking_condition", condition)
                data.setdefault("retryable", True)
                return {
                    **result,
                    "ok": False,
                    "action": tool_name,
                    "summary": "Could not verify app focus before foreground action",
                    "error": "app_focus_not_verified",
                    "blocking_condition": condition,
                    "blocking_conditions": _foreground_blocking_conditions(result, condition),
                    "retryable": True,
                    "data": data,
                    "recovery_actions": _foreground_focus_recovery_actions(clean_app_name),
                    "fallback_used": fallback_used,
                    "fallback_result": dict(step_results),
                }
            if not result.get("ok"):
                result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
                data = dict(result_data)
                if clean_app_name:
                    data["app_name"] = clean_app_name
                return {
                    **result,
                    "action": tool_name,
                    "summary": f"Could not {step_name} app before foreground action",
                    "data": data,
                    "fallback_used": fallback_used,
                    "fallback_result": dict(step_results),
                }

        action_name, action, *action_options = action_step
        expected_app_name = _foreground_expected_app_name(clean_app_name, step_results)
        active_window_result = desktop.active_window()
        step_results["active_window"] = active_window_result
        fallback_used = fallback_used or bool(active_window_result.get("fallback_used"))
        active_window_data = (
            active_window_result.get("data")
            if isinstance(active_window_result.get("data"), dict)
            else {}
        )
        active_app_name = str(
            active_window_data.get("app_name")
            or active_window_data.get("frontmost_app")
            or ""
        ).strip()
        if active_window_result.get("ok") is not True or not _app_names_match(
            expected_app_name,
            active_app_name,
        ):
            data = dict(active_window_data)
            if clean_app_name:
                data["app_name"] = clean_app_name
            data["expected_app_name"] = expected_app_name
            data["active_app_name"] = active_app_name
            data["foreground_action"] = action_name
            condition = _foreground_blocking_condition(active_window_result)
            data.setdefault("blocking_condition", condition)
            data.setdefault("retryable", True)
            return {
                **active_window_result,
                "ok": False,
                "action": tool_name,
                "summary": "Could not verify target app is foreground before foreground action",
                "error": (
                    str(active_window_result.get("error") or "")
                    or "foreground_app_mismatch"
                ),
                "blocking_condition": condition,
                "blocking_conditions": _foreground_blocking_conditions(
                    active_window_result,
                    condition,
                ),
                "retryable": True,
                "data": data,
                "recovery_actions": _foreground_focus_recovery_actions(clean_app_name),
                "fallback_used": fallback_used,
                "fallback_result": dict(step_results),
            }

        action_result = action(expected_app_name) if action_options else action()
        action_data = action_result.get("data") if isinstance(action_result.get("data"), dict) else {}
        data = dict(action_data)
        if clean_app_name:
            data["app_name"] = clean_app_name
        data["foreground_action"] = action_name
        fallback_used = fallback_used or bool(action_result.get("fallback_used"))
        fallback_result = {**step_results, action_name: action_result}
        if action_result.get("ok"):
            return {
                **action_result,
                "action": tool_name,
                "summary": "Focused app and completed foreground action",
                "data": data,
                "fallback_used": fallback_used,
                "fallback_result": fallback_result,
            }
        return {
            **action_result,
            "action": tool_name,
            "summary": "Focused app but could not complete foreground action",
            "data": data,
            "fallback_used": fallback_used,
            "fallback_result": fallback_result,
        }

    def browser_open_url(
        self,
        url: str,
        *,
        allow_system_browser_fallback: bool = False,
    ) -> dict[str, Any]:
        # Opening a new target starts a new ownership chain.  Close only the
        # exact prior run-owned target, then clear ownership before creating a
        # replacement so no failure can fall back to stale user state.
        previous_target_id = str(self._owned_browser_target_id or "").strip()
        self._owned_browser_target_id = ""
        previous_target_cleanup = (
            browser.close_target(previous_target_id)
            if browser.is_valid_target_id(previous_target_id)
            else None
        )
        if allow_system_browser_fallback:
            result = browser.open_url(
                url,
                allow_system_browser_fallback=True,
            )
        else:
            result = browser.open_url(url)
        if not result.get("ok") or result.get("fallback_used"):
            return {
                **result,
                "browser_target_ownership_cleared": True,
            }
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        target_id = str(data.get("target_id") or "").strip()
        target_websocket_available = data.get("target_websocket_available") is True
        if not browser.is_valid_target_id(target_id) or not target_websocket_available:
            return {
                "ok": False,
                "status": "blocked",
                "action": "browser.open_url",
                "summary": "Browser target ownership could not be verified",
                "error": "browser_owned_target_unverified",
                "permission_error": False,
                "fallback_used": False,
                "blocking_conditions": ["browser_owned_target_required"],
                "user_handoff_required": True,
                "replan_allowed": False,
                "fallback_result": {"open": result},
            }
        self._owned_browser_target_id = target_id
        return {
            **result,
            # The broker has just validated both the run-owned target identity
            # and its page websocket.  Project that read-after-open receipt so
            # the generic GoalContract can prove the browser.open_url effect
            # without asking the model to restate an already completed action.
            "postcondition_verified": True,
            "data": {
                **data,
                "target_id": target_id,
                "target_owned_by_run": True,
                "postcondition_verified": True,
                "browser_profile_isolated": True,
                "browser_profile_isolated_from_user": True,
                **(
                    {"previous_target_cleanup": previous_target_cleanup}
                    if previous_target_cleanup is not None
                    else {}
                ),
            },
        }

    def close_owned_browser_target(self) -> dict[str, Any]:
        """Release this broker's exact CDP target while preserving user tabs."""

        target_id = str(self._owned_browser_target_id or "").strip()
        self._owned_browser_target_id = ""
        if not browser.is_valid_target_id(target_id):
            return {
                "ok": True,
                "action": "browser.close_target",
                "summary": "No run-owned browser target to close",
                "data": {"already_closed": True},
                "fallback_used": False,
            }
        return browser.close_target(target_id)

    def restore_owned_browser_target(self, target_id: str) -> None:
        """Restore a target only from trusted run history during approval resume."""

        clean_target_id = str(target_id or "").strip()
        if not browser.is_valid_target_id(clean_target_id):
            self._owned_browser_target_id = ""
            return
        self._owned_browser_target_id = clean_target_id

    def browser_current_page(self) -> dict[str, Any]:
        return self._with_owned_browser_target(
            "browser.current_page",
            browser.current_page,
        )

    def browser_click(
        self,
        selector: str,
        *,
        fallback_x: Any = None,
        fallback_y: Any = None,
        click_count: Any = 1,
        allow_foreground_fallback: bool = False,
    ) -> dict[str, Any]:
        foreground_fallback = None
        if allow_foreground_fallback:
            foreground_fallback = lambda x, y, count: self._with_foreground_lock(
                "browser.click",
                lambda: desktop.desktop_click(x, y, click_count=count),
            )
        action = lambda: browser.click(
            selector,
            fallback_x=fallback_x,
            fallback_y=fallback_y,
            click_count=click_count,
            foreground_fallback=foreground_fallback,
        )
        if allow_foreground_fallback and not self._owned_browser_target_id:
            return action()
        return self._with_owned_browser_target("browser.click", action)

    def browser_type_text(
        self,
        selector: str,
        text: str,
        *,
        fallback_x: Any = None,
        fallback_y: Any = None,
        allow_foreground_fallback: bool = False,
    ) -> dict[str, Any]:
        def foreground_fallback(*args: Any) -> dict[str, Any]:
            return self._with_foreground_lock(
                "browser.type_text",
                lambda: browser._type_text_foreground_fallback(*args),
            )

        action = lambda: browser.type_text(
            selector,
            text,
            fallback_x=fallback_x,
            fallback_y=fallback_y,
            foreground_fallback=(
                foreground_fallback if allow_foreground_fallback else None
            ),
        )
        if allow_foreground_fallback and not self._owned_browser_target_id:
            return action()
        return self._with_owned_browser_target("browser.type_text", action)

    def browser_extract_text(self, selector: str = "") -> dict[str, Any]:
        return self._with_owned_browser_target(
            "browser.extract_text",
            lambda: browser.extract_text(selector),
        )

    def browser_open_url_and_extract_text(
        self,
        url: str,
        *,
        selector: str = "",
    ) -> dict[str, Any]:
        open_result = self.browser_open_url(url)
        open_data = open_result.get("data") if isinstance(open_result.get("data"), dict) else {}
        opened_url = str(open_data.get("url") or url or "").strip()
        if not open_result.get("ok"):
            data = dict(open_data)
            if opened_url:
                data["url"] = opened_url
            return {
                **open_result,
                "action": "browser.open_url_and_extract_text",
                "summary": open_result.get("summary") or "Could not open browser page before extracting text",
                "data": data,
                "fallback_result": {"open": open_result},
            }

        extract_result = self.browser_extract_text(selector)
        extract_data = extract_result.get("data") if isinstance(extract_result.get("data"), dict) else {}
        data = dict(extract_data)
        if opened_url:
            data["url"] = opened_url
        data["selector"] = str(selector or "")
        fallback_used = bool(open_result.get("fallback_used") or extract_result.get("fallback_used"))
        if extract_result.get("ok"):
            result = {
                **extract_result,
                "action": "browser.open_url_and_extract_text",
                "summary": extract_result.get("summary") or "Opened browser page and extracted text",
                "data": data,
                "fallback_used": fallback_used,
            }
            if open_result.get("fallback_used"):
                result["fallback_result"] = {"open": open_result}
            return result
        return {
            **extract_result,
            "action": "browser.open_url_and_extract_text",
            "summary": "Opened browser page but could not extract text",
            "data": data,
            "fallback_used": fallback_used,
            "fallback_result": {"open": open_result, "extract_text": extract_result},
        }

    def browser_screenshot(
        self,
        *,
        reason: str = "",
        allow_screen_fallback: bool = False,
    ) -> dict[str, Any]:
        rel = Path("browser") / "current-page.png"
        target = (self.artifact_root / rel).resolve()
        if not _is_within(target, self.artifact_root):
            raise AgentRuntimeError("browser artifact 路径越界")
        target.parent.mkdir(parents=True, exist_ok=True)
        if allow_screen_fallback:
            action = lambda: browser.screenshot(
                target,
                allow_screen_fallback=True,
            )
        else:
            action = lambda: browser.screenshot(target)
        if allow_screen_fallback and not self._owned_browser_target_id:
            result = action()
        else:
            result = self._with_owned_browser_target(
                "browser.screenshot",
                action,
            )
        if not result.get("ok"):
            return result
        data = dict(result.get("data") or {})
        data["path"] = str(rel)
        return {
            **result,
            "summary": result.get("summary") or "Captured current browser page",
            "reason": str(reason or "").strip(),
            "artifact": {
                "path": str(rel),
                "kind": "image",
                "mime_type": data.get("mime_type") or "image/png",
                "size_bytes": data.get("size") or data.get("size_bytes"),
            },
            "data": data,
        }

    def browser_open_url_and_screenshot(self, url: str, *, reason: str = "") -> dict[str, Any]:
        open_result = self.browser_open_url(url)
        open_data = open_result.get("data") if isinstance(open_result.get("data"), dict) else {}
        opened_url = str(open_data.get("url") or url or "").strip()
        if not open_result.get("ok"):
            data = dict(open_data)
            if opened_url:
                data["url"] = opened_url
            return {
                **open_result,
                "action": "browser.open_url_and_screenshot",
                "summary": open_result.get("summary") or "Could not open browser page before screenshot",
                "data": data,
                "fallback_result": {"open": open_result},
            }

        screenshot_result = self.browser_screenshot(reason=reason)
        screenshot_data = (
            screenshot_result.get("data") if isinstance(screenshot_result.get("data"), dict) else {}
        )
        data = dict(screenshot_data)
        if opened_url:
            data["url"] = opened_url
        fallback_used = bool(open_result.get("fallback_used") or screenshot_result.get("fallback_used"))
        if screenshot_result.get("ok"):
            result = {
                **screenshot_result,
                "action": "browser.open_url_and_screenshot",
                "summary": screenshot_result.get("summary") or "Opened browser page and captured screenshot",
                "data": data,
                "fallback_used": fallback_used,
            }
            if open_result.get("fallback_used"):
                result["fallback_result"] = {"open": open_result}
            return result
        return {
            **screenshot_result,
            "action": "browser.open_url_and_screenshot",
            "summary": "Opened browser page but could not capture screenshot",
            "data": data,
            "fallback_used": fallback_used,
            "fallback_result": {"open": open_result, "screenshot": screenshot_result},
        }

    def call(self, name: str, payload: dict[str, Any], *, approved: bool = False) -> dict[str, Any]:
        precondition_failure = self.tool_precondition_failure(name)
        if precondition_failure is not None:
            return precondition_failure
        if not approved and self.approvals.get(name) and name not in {
            "file.organize",
            "terminal.run",
            "workspace.write_patch",
        }:
            return {
                "ok": False,
                "approval_required": True,
                "tool": name,
                "policy_reason": "当前工具策略要求人工确认后再执行。",
            }
        return dispatch_tool_call(self, name, payload, approved=approved)

    def tool_precondition_failure(self, name: str) -> dict[str, Any] | None:
        """Return a safe blocker before policy asks for an impossible approval."""

        clean_name = str(name or "").strip()
        if clean_name not in _BROWSER_OWNED_TARGET_REQUIRED_TOOLS:
            return None
        if browser.is_valid_target_id(self._owned_browser_target_id):
            return None
        self._owned_browser_target_id = ""
        return self._browser_owned_target_required_result(clean_name)

    @staticmethod
    def _browser_owned_target_required_result(action_name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "blocked",
            "action": action_name,
            "summary": "Open a run-owned browser page before using this browser tool",
            "error": "browser_owned_target_required",
            "permission_error": False,
            "fallback_used": False,
            "blocking_conditions": ["browser_owned_target_required"],
            "user_handoff_required": True,
            "replan_allowed": False,
            "recovery_hints": [
                "请先让 Yachiyo 打开目标网页，再继续读取或操作；它不会接管你当前正在使用的浏览器标签页。"
            ],
        }

    def _with_owned_browser_target(
        self,
        action_name: str,
        action: Any,
    ) -> dict[str, Any]:
        target_id = str(self._owned_browser_target_id or "").strip()
        if not browser.is_valid_target_id(target_id):
            self._owned_browser_target_id = ""
            return self._browser_owned_target_required_result(action_name)
        try:
            with browser.owned_browser_target(target_id):
                result = action()
        except Exception as exc:
            self._owned_browser_target_id = ""
            return {
                "ok": False,
                "status": "blocked",
                "action": action_name,
                "summary": "Run-owned browser target is unavailable",
                "error": "browser_owned_target_unavailable",
                "detail": str(exc),
                "permission_error": False,
                "fallback_used": False,
                "blocking_conditions": ["browser_owned_target_required"],
                "user_handoff_required": True,
                "replan_allowed": False,
            }
        payload = dict(result) if isinstance(result, dict) else {
            "ok": False,
            "action": action_name,
            "error": "browser_target_result_invalid",
        }
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        payload["data"] = {
            **data,
            "target_id": target_id,
            "target_owned_by_run": True,
            "browser_profile_isolated": True,
            "browser_profile_isolated_from_user": True,
        }
        detail = str(payload.get("detail") or "")
        if "Run-owned browser target is unavailable" in detail:
            self._owned_browser_target_id = ""
            payload["browser_target_ownership_cleared"] = True
            payload["data"] = {
                **payload["data"],
                "target_owned_by_run": False,
            }
        return payload

    def _with_foreground_lock(self, tool_name: str, action: Any) -> dict[str, Any]:
        if self.foreground_lock is None:
            return action()
        holder = str(self.foreground_lock_owner or self.artifact_root).strip()
        lease = self.foreground_lock.acquire(holder=holder, tool_name=tool_name)
        if not lease.acquired:
            return {
                "ok": False,
                "tool": tool_name,
                "action": "foreground_lock",
                "foreground_lock_busy": True,
                "locked_by": lease.locked_by,
                "summary": "Foreground desktop action is already locked by another run.",
            }
        try:
            result = action()
            if isinstance(result, dict):
                return {
                    **result,
                    "foreground_lock": {
                        "holder": holder,
                        "tool": tool_name,
                    },
                }
            return result
        finally:
            lease.release()


def _foreground_focus_not_verified(step_name: str, result: dict[str, Any]) -> bool:
    if step_name != "focus":
        return False
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return data.get("focus_verified") is False


def _foreground_blocking_condition(result: Mapping[str, Any]) -> str:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    return str(
        result.get("blocking_condition")
        or data.get("blocking_condition")
        or "foreground_focus_unavailable"
    ).strip()


def _foreground_blocking_conditions(result: Mapping[str, Any], condition: str) -> list[str]:
    values = _clean_string_list(result.get("blocking_conditions"))
    if condition and condition not in values:
        values.append(condition)
    return values


def _foreground_focus_recovery_actions(app_name: str) -> list[dict[str, Any]]:
    clean_name = str(app_name or "").strip()
    actions: list[dict[str, Any]] = []
    if clean_name:
        actions.append(
            {
                "label": f"重新打开{clean_name}",
                "tool": "app.open",
                "input": {"app_name": clean_name},
                "permission_target": "foreground_focus",
                "risk_level": "low",
            }
        )
    actions.extend(
        [
            {
                "label": "查看前台窗口",
                "tool": "desktop.active_window",
                "input": {},
                "permission_target": "foreground_focus",
                "risk_level": "low",
            },
            {
                "label": "截图确认前台",
                "tool": "screen.capture",
                "input": {"reason": "verify foreground app after focus failure"},
                "permission_target": "foreground_focus",
                "risk_level": "low",
            },
            {
                "label": "打开自动化权限",
                "tool": "system.settings_open",
                "input": {"target": "自动化权限"},
                "permission_target": "automation",
                "risk_level": "low",
            },
            {
                "label": "打开辅助功能权限",
                "tool": "system.settings_open",
                "input": {"target": "辅助功能权限"},
                "permission_target": "accessibility",
                "risk_level": "low",
            },
        ]
    )
    return actions
