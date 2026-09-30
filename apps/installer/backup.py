"""Oha-Yachiyo local data backup and restore."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from apps.shell import config as config_mod
from apps.shell.assets import project_display_path

logger = logging.getLogger(__name__)

BACKUP_DIR_NAME = "Oha-Yachiyo-backups"
BACKUP_FILE_PREFIX = "oha-yachiyo-backup-"
BACKUP_SCHEMA_VERSION = 2
MANIFEST_NAME = "manifest.json"
DEFAULT_RETENTION_COUNT = 10
MAX_BACKUP_IMPORT_ENTRY_BYTES = 512 * 1024 * 1024
MAX_BACKUP_IMPORT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_ZIP_COPY_CHUNK_BYTES = 1024 * 1024
_RUNTIME_SQLITE_DATABASES = frozenset({
    "chat.db", "activity.db", "agent-runtime.db", "model-profiles.db",
})
_RUNTIME_SQLITE_SIDECARS = frozenset(
    f"{name}{suffix}"
    for name in _RUNTIME_SQLITE_DATABASES
    for suffix in ("-wal", "-shm")
)
_BACKUP_ARCHIVE_NAME_RE = re.compile(
    rf"^{re.escape(BACKUP_FILE_PREFIX)}\d{{8}}-\d{{6}}(?:-(\d+))?\.zip$"
)


@dataclass(frozen=True)
class BackupInfo:
    """备份文件信息。"""

    path: str
    display_path: str
    created_at: str = ""
    scope: str = ""
    schema_version: int = 0
    format: str = ""
    included: list[str] = field(default_factory=list)
    size_bytes: int = 0
    size_display: str = ""
    valid: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackupImportResult:
    """备份导入结果。"""

    ok: bool
    backup: BackupInfo | None = None
    restored: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    restart_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        backup = self.backup.to_dict() if self.backup else None
        return {
            "ok": self.ok,
            "backup": backup,
            "restored": self.restored,
            "skipped": self.skipped,
            "errors": self.errors,
            "restart_required": self.restart_required,
        }


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _display_path(path: Path) -> str:
    try:
        return project_display_path(path)
    except Exception:
        return str(path.expanduser())


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@lru_cache(maxsize=16)
def _protected_paths_for_home(home: Path) -> frozenset[Path]:
    candidates = {
        Path("/"),
        home,
        home.parent,
        Path("/Applications"),
        Path("/Library"),
        Path("/System"),
        Path("/bin"),
        Path("/etc"),
        Path("/opt"),
        Path("/sbin"),
        Path("/usr"),
        Path("/var"),
    }
    return frozenset(path.resolve() for path in candidates if path.exists())


def _current_protected_paths() -> frozenset[Path]:
    home = Path.home().expanduser().resolve()
    return _protected_paths_for_home(home)


def protected_paths() -> set[Path]:
    """返回不允许备份恢复流程删除或替换的受保护路径集合。"""
    return set(_current_protected_paths())


def is_protected_path(path: Path) -> bool:
    """判断路径是否命中受保护路径。"""
    try:
        resolved = path.expanduser().resolve()
    except Exception:
        return True
    return resolved in _current_protected_paths()


def is_safe_app_config_dir(path: Path) -> tuple[bool, str]:
    """判断目标路径是否可作为 Oha-Yachiyo 应用配置目录安全删除或替换。"""
    original = path.expanduser()
    if original.name != ".oha-yachiyo-config":
        return False, "配置目录名称不符合预期，已跳过"

    home = Path.home().expanduser().resolve()
    if not _is_relative_to(original.parent, home):
        return False, "配置目录不在当前用户目录下，已跳过"

    if original.is_symlink():
        return True, ""

    resolved = original.resolve()
    if is_protected_path(resolved):
        return False, "受保护路径，已跳过"
    if not _is_relative_to(resolved, home):
        return False, "配置目录不在当前用户目录下，已跳过"
    return True, ""


def _has_oha_workspace_marker(path: Path) -> bool:
    native_marker = path / ".oha_yachiyo_init"
    if native_marker.is_file() and not native_marker.is_symlink():
        return True
    config_marker = path / "configs" / "oha-yachiyo.json"
    return (
        config_marker.is_file()
        and not config_marker.is_symlink()
        and not config_marker.parent.is_symlink()
    )


def is_safe_oha_workspace(path: Path) -> tuple[bool, str]:
    """判断目标路径是否可作为 Oha-Yachiyo 工作空间安全删除或替换。"""
    original = path.expanduser()
    if original.name != ".oha-yachiyo":
        return False, "工作空间目录名称不符合预期，已跳过"

    home = Path.home().expanduser().resolve()
    if not _is_relative_to(original.parent, home):
        return False, "工作空间不在当前用户目录下，已跳过"
    if original.is_symlink():
        return True, ""

    resolved = original.resolve()
    if is_protected_path(resolved):
        return False, "受保护路径，已跳过"
    if not _is_relative_to(resolved, home):
        return False, "工作空间不在当前用户目录下，已跳过"
    if _path_exists(original) and not _has_oha_workspace_marker(resolved):
        return False, "工作空间缺少 Oha-Yachiyo 初始化标识，已跳过"
    return True, ""


def _app_config_dir() -> Path:
    return Path(getattr(config_mod, "_CONFIG_DIR")).expanduser()


def _oha_yachiyo_home_dir() -> Path:
    return Path(os.getenv("OHA_YACHIYO_HOME", "~/.oha-yachiyo")).expanduser()


def _oha_workspace_dir() -> Path:
    return _oha_yachiyo_home_dir()


def default_backup_root(backup_root: str | Path | None = None) -> Path:
    """返回默认备份目录。"""
    if backup_root is not None:
        return Path(backup_root).expanduser()
    return Path.home().expanduser() / BACKUP_DIR_NAME


def _unique_backup_archive(root: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = root / f"{BACKUP_FILE_PREFIX}{timestamp}.zip"
    existing_orders = [
        order
        for path in root.glob(f"{BACKUP_FILE_PREFIX}{timestamp}*.zip")
        if path.is_file()
        for order in [_filename_order(path)]
        if order > 0
    ]
    if not existing_orders and not base.exists():
        return base
    suffix = max(existing_orders, default=1) + 1
    while True:
        candidate = root / f"{BACKUP_FILE_PREFIX}{timestamp}-{suffix}.zip"
        if not candidate.exists():
            return candidate
        suffix += 1


def _copy_sqlite_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as source_conn:
            with closing(sqlite3.connect(target)) as target_conn:
                source_conn.backup(target_conn)
        shutil.copystat(source, target, follow_symlinks=False)
    except sqlite3.Error:
        shutil.copy2(source, target, follow_symlinks=False)


def _copy_file(source: str, target: str) -> str:
    source_path = Path(source)
    target_path = Path(target)
    if source_path.is_symlink():
        logger.warning("跳过符号链接备份项: %s", source_path)
        return target
    if source_path.name in _RUNTIME_SQLITE_DATABASES:
        _copy_sqlite_database(source_path, target_path)
    else:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path, follow_symlinks=False)
    return target


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    # Each database is copied through SQLite's online backup API. Copying its
    # live WAL afterwards could overwrite the snapshot with a different state.
    ignored = set(names) & _RUNTIME_SQLITE_SIDECARS
    directory = Path(_directory)
    for name in names:
        if name in ignored:
            continue
        path = directory / name
        if path.is_symlink():
            logger.warning("跳过符号链接备份项: %s", path)
            ignored.add(name)
    return ignored


def _copy_path(source: Path, target: Path) -> bool:
    if not _path_exists(source):
        return False
    if source.is_symlink():
        logger.warning("跳过符号链接备份源: %s", source)
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, copy_function=_copy_file, ignore=_copy_ignore)
    else:
        _copy_file(str(source), str(target))
    return True


def _write_zip_from_dir(source_dir: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_symlink():
                logger.warning("跳过符号链接压缩项: %s", path)
                continue
            relative = path.relative_to(source_dir).as_posix()
            if path.is_dir():
                archive.write(path, relative + "/")
            else:
                archive.write(path, relative)


def _remove_file_if_exists(path: Path) -> None:
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
    except Exception:
        logger.warning("清理临时备份文件失败: %s", path, exc_info=True)


def create_backup(
    *,
    backup_root: str | Path | None = None,
    source_context: str = "manual",
    auto_cleanup: bool = True,
    retention_count: int = DEFAULT_RETENTION_COUNT,
    overwrite_latest: bool = False,
) -> BackupInfo:
    """生成可独立触发的 Oha-Yachiyo 本地资料备份。"""
    root = default_backup_root(backup_root)
    root.mkdir(parents=True, exist_ok=True)
    previous_latest = find_latest_backup(root) if overwrite_latest else None
    archive_path = _unique_backup_archive(root)
    temp_archive_path = root / f".{archive_path.name}.{uuid.uuid4().hex}.tmp"
    archive_published = False
    staging_root = Path(tempfile.mkdtemp(prefix=".oha-yachiyo-backup-", dir=str(root)))
    staging_dir = staging_root / "payload"
    staging_dir.mkdir(parents=True, exist_ok=False)

    try:
        sources: list[tuple[str, str, Path, str]] = [
            (
                "app_config",
                "Oha-Yachiyo 应用配置",
                _app_config_dir(),
                "app-config",
            ),
            (
                "oha_workspace",
                "Oha-Yachiyo 工作空间",
                _oha_workspace_dir(),
                "oha-workspace",
            ),
        ]
        copied: list[dict[str, str]] = []
        for source_id, label, source_path, target_name in sources:
            if _copy_path(source_path, staging_dir / target_name):
                copied.append({
                    "id": source_id,
                    "label": label,
                    "target": target_name,
                })

        if not copied:
            raise ValueError("未找到可备份的 Oha-Yachiyo 本地资料")

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "kind": "oha-yachiyo-backup",
            "format": "zip",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": source_context,
            "entries": copied,
            "note": (
                "此备份包含 Oha-Yachiyo 应用配置与工作空间，"
                "包括聊天数据库、项目资料、缓存、日志与导入资源。"
            ),
        }
        (staging_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _write_zip_from_dir(staging_dir, temp_archive_path)
        temp_archive_path.replace(archive_path)
        archive_published = True
        backup = _backup_info(archive_path)
        if not backup.valid:
            raise ValueError(f"创建的备份文件无效：{backup.error}")
        if auto_cleanup:
            cleanup_old_backups(backup_root=root, keep_count=retention_count)
        if (
            previous_latest
            and previous_latest.path != backup.path
            and Path(previous_latest.path).exists()
        ):
            delete_backup(previous_latest.path, backup_root=root)
        return backup
    except Exception:
        _remove_file_if_exists(temp_archive_path)
        if archive_published:
            _remove_file_if_exists(archive_path)
        raise
    finally:
        _remove_file_if_exists(temp_archive_path)
        shutil.rmtree(staging_root, ignore_errors=True)


def _read_manifest_from_directory(snapshot_dir: Path) -> dict[str, Any]:
    manifest_path = snapshot_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError("缺少 manifest.json")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest.json 格式无效")
    return data


def _read_manifest_from_zip(archive_path: Path) -> dict[str, Any]:
    if not zipfile.is_zipfile(archive_path):
        raise ValueError("备份文件不是有效 ZIP")
    with zipfile.ZipFile(archive_path, "r") as archive:
        try:
            raw = archive.read(MANIFEST_NAME)
        except KeyError as exc:
            raise ValueError("缺少 manifest.json") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest.json 格式无效")
    return data


def _read_backup_manifest(path: Path) -> dict[str, Any]:
    if path.is_file():
        data = _read_manifest_from_zip(path)
        data.setdefault("format", "zip")
    elif path.is_dir():
        data = _read_manifest_from_directory(path)
        data.setdefault("format", "directory")
    else:
        raise ValueError("备份路径不存在")

    schema_version = int(data.get("schema_version") or 0)
    if schema_version != BACKUP_SCHEMA_VERSION:
        raise ValueError("不支持的备份版本")
    return data


def _included_ids(path: Path, data: dict[str, Any]) -> list[str]:
    entries = data.get("entries") or data.get("copied")
    if isinstance(entries, list):
        return [
            str(item.get("id"))
            for item in entries
            if isinstance(item, dict) and item.get("id")
        ]

    included: list[str] = []
    if path.is_dir():
        if (path / "app-config").exists():
            included.append("app_config")
        if (path / "oha-workspace").exists():
            included.append("oha_workspace")
    elif path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
        if any(name.startswith("app-config/") for name in names):
            included.append("app_config")
        if any(name.startswith("oha-workspace/") for name in names):
            included.append("oha_workspace")
    return included


def _backup_info(path: Path) -> BackupInfo:
    resolved = path.expanduser().resolve()
    size_bytes = _backup_size(resolved)
    size_display = _format_bytes(size_bytes)
    try:
        data = _read_backup_manifest(resolved)
        included = _included_ids(resolved, data)
        if not included:
            raise ValueError("备份内没有可导入内容")
        return BackupInfo(
            path=str(resolved),
            display_path=_display_path(resolved),
            created_at=str(data.get("created_at") or ""),
            scope=str(data.get("scope") or ""),
            schema_version=int(data.get("schema_version") or 0),
            format=str(data.get("format") or ""),
            included=included,
            size_bytes=size_bytes,
            size_display=size_display,
        )
    except Exception as exc:
        return BackupInfo(
            path=str(resolved),
            display_path=_display_path(resolved),
            size_bytes=size_bytes,
            size_display=size_display,
            valid=False,
            error=str(exc),
        )


def _looks_like_backup(path: Path) -> bool:
    return path.is_file() and _BACKUP_ARCHIVE_NAME_RE.fullmatch(path.name) is not None


def _is_restore_source_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _backup_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _format_bytes(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size_bytes} B"


def _filename_order(path: Path) -> int:
    match = _BACKUP_ARCHIVE_NAME_RE.fullmatch(path.name)
    if match is None:
        return 0
    suffix = match.group(1)
    return int(suffix) if suffix else 1


def find_backups(backup_root: str | Path | None = None) -> list[BackupInfo]:
    """列出可导入备份，最新在前。"""
    backups: list[BackupInfo] = []
    root = default_backup_root(backup_root)
    if not root.exists() or not root.is_dir():
        return backups
    for path in root.iterdir():
        if _looks_like_backup(path):
            backups.append(_backup_info(path))

    def sort_key(item: BackupInfo) -> tuple[str, int, int, str]:
        path = Path(item.path)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except Exception:
            mtime_ns = 0
        if item.created_at:
            return (item.created_at, mtime_ns, _filename_order(path), path.name)
        try:
            created_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except Exception:
            created_at = ""
        return (created_at, mtime_ns, _filename_order(path), path.name)

    backups.sort(key=sort_key, reverse=True)
    return backups


def find_latest_backup(backup_root: str | Path | None = None) -> BackupInfo | None:
    """返回最近一个有效备份。"""
    for backup in find_backups(backup_root):
        if backup.valid:
            return backup
    return None


def get_backup_status(backup_root: str | Path | None = None) -> dict[str, Any]:
    """获取备份目录状态，供主界面与安装引导展示。"""
    root = default_backup_root(backup_root)
    backups = find_backups(backup_root)
    latest = next((item for item in backups if item.valid), None)
    latest_dict = latest.to_dict() if latest else None
    total_size = sum(item.size_bytes for item in backups)
    return {
        "backup_root": str(root),
        "backup_root_display": _display_path(root),
        "backups": [item.to_dict() for item in backups],
        "latest": latest_dict,
        "has_backup": latest is not None,
        "count": len(backups),
        "total_size_bytes": total_size,
        "total_size_display": _format_bytes(total_size),
    }


def cleanup_old_backups(
    *,
    backup_root: str | Path | None = None,
    keep_count: int = DEFAULT_RETENTION_COUNT,
) -> list[BackupInfo]:
    """删除超过保留数量的旧托管备份，返回被删除的备份信息。"""
    keep_count = max(1, int(keep_count))
    backups = find_backups(backup_root)
    deleted: list[BackupInfo] = []
    for backup in backups[keep_count:]:
        try:
            delete_backup(backup.path, backup_root=backup_root)
        except ValueError as exc:
            logger.warning("跳过不可管理的备份文件 %s: %s", backup.path, exc)
            continue
        deleted.append(backup)
    return deleted


def resolve_managed_backup_path(
    backup_path: str | Path,
    *,
    backup_root: str | Path | None = None,
) -> Path:
    """解析并校验默认备份目录内的托管备份 ZIP 文件。"""
    root = default_backup_root(backup_root).expanduser().resolve()
    path = Path(backup_path).expanduser().resolve()
    if path.parent != root:
        raise ValueError("只能管理默认备份目录中的备份文件")
    if _BACKUP_ARCHIVE_NAME_RE.fullmatch(path.name) is None:
        raise ValueError("备份文件名称不符合预期")
    if not path.is_file():
        raise ValueError("备份文件不存在")
    return path


def delete_backup(
    backup_path: str | Path,
    *,
    backup_root: str | Path | None = None,
) -> BackupInfo:
    """删除指定备份。"""
    path = resolve_managed_backup_path(backup_path, backup_root=backup_root)
    info = _backup_info(path)
    path.unlink()
    return info


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _copy_zip_member_bounded(
    source_file: Any,
    output_path: Path,
    total_bytes_written: int,
) -> int:
    entry_bytes_written = 0
    try:
        with output_path.open("wb") as target_file:
            while True:
                chunk = source_file.read(_ZIP_COPY_CHUNK_BYTES)
                if not chunk:
                    break

                next_entry_size = entry_bytes_written + len(chunk)
                if next_entry_size > MAX_BACKUP_IMPORT_ENTRY_BYTES:
                    raise ValueError("备份文件包含过大的单个条目")

                next_total_size = total_bytes_written + len(chunk)
                if next_total_size > MAX_BACKUP_IMPORT_TOTAL_BYTES:
                    raise ValueError("备份文件解压后体积超过限制")

                target_file.write(chunk)
                entry_bytes_written = next_entry_size
                total_bytes_written = next_total_size
    except Exception:
        _remove_file_if_exists(output_path)
        raise
    return total_bytes_written


def _replace_path(source: Path, target: Path) -> bool:
    if not _path_exists(source):
        return False
    target_parent = target.parent
    target_parent.mkdir(parents=True, exist_ok=True)

    staging_root = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=str(target_parent)))
    staged_target = staging_root / target.name
    rollback_path: Path | None = None

    try:
        if not _copy_path(source, staged_target):
            return False

        if _path_exists(target):
            rollback_path = target_parent / f".{target.name}.rollback-{uuid.uuid4().hex}"
            shutil.move(str(target), str(rollback_path))

        try:
            shutil.move(str(staged_target), str(target))
        except Exception:
            if _path_exists(target):
                _remove_path(target)
            if rollback_path is not None and _path_exists(rollback_path):
                shutil.move(str(rollback_path), str(target))
            raise

        if rollback_path is not None and _path_exists(rollback_path):
            _remove_path(rollback_path)
        return True
    finally:
        if _path_exists(staging_root):
            _remove_path(staging_root)


def _extract_zip_safely(archive_path: Path, target_dir: Path) -> None:
    with zipfile.ZipFile(archive_path, "r") as archive:
        total_uncompressed_size = 0
        total_bytes_written = 0
        seen_member_paths: set[PurePosixPath] = set()
        for member in archive.infolist():
            member_name = member.filename.replace("\\", "/")
            if re.match(r"^[A-Za-z]:", member_name) or member_name.startswith("//"):
                raise ValueError("备份文件包含不安全路径")
            member_path = PurePosixPath(member_name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("备份文件包含不安全路径")
            if member_path in seen_member_paths:
                raise ValueError("备份文件包含重复条目")
            seen_member_paths.add(member_path)
            if member.file_size > MAX_BACKUP_IMPORT_ENTRY_BYTES:
                raise ValueError("备份文件包含过大的单个条目")
            total_uncompressed_size += member.file_size
            if total_uncompressed_size > MAX_BACKUP_IMPORT_TOTAL_BYTES:
                raise ValueError("备份文件解压后体积超过限制")
            output_path = target_dir.joinpath(*member_path.parts)
            if member.is_dir():
                output_path.mkdir(parents=True, exist_ok=True)
                continue
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source_file:
                total_bytes_written = _copy_zip_member_bounded(
                    source_file,
                    output_path,
                    total_bytes_written,
                )


@contextmanager
def _materialized_backup(path: Path) -> Iterator[Path]:
    if path.is_dir():
        yield path
        return

    with tempfile.TemporaryDirectory(prefix="oha-yachiyo-import-") as temp_dir:
        target_dir = Path(temp_dir) / "payload"
        target_dir.mkdir(parents=True, exist_ok=True)
        _extract_zip_safely(path, target_dir)
        yield target_dir


def import_backup(
    backup_path: str | Path | None = None,
    *,
    backup_root: str | Path | None = None,
) -> BackupImportResult:
    """导入 Oha-Yachiyo 备份。"""
    if backup_path:
        backup_source = Path(backup_path).expanduser()
        if not _looks_like_backup(backup_source):
            info = BackupInfo(
                path=str(backup_source),
                display_path=_display_path(backup_source),
                valid=False,
                error="只支持导入新版 ZIP 备份文件",
            )
            return BackupImportResult(ok=False, backup=info, errors=[info.error])
        backup = _backup_info(backup_source)
    else:
        backup = None
    if backup is None:
        backup = find_latest_backup(backup_root)
    if backup is None:
        return BackupImportResult(ok=False, errors=["未找到可导入的备份"])
    if not backup.valid:
        return BackupImportResult(ok=False, backup=backup, errors=[backup.error])

    backup_source = Path(backup.path).expanduser().resolve()
    restored: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    errors: list[str] = []

    with _materialized_backup(backup_source) as snapshot_dir:
        _read_backup_manifest(snapshot_dir)

        app_config_source = snapshot_dir / "app-config"
        app_config_target = _app_config_dir()
        app_config_safe, app_config_reason = is_safe_app_config_dir(app_config_target)
        if not app_config_source.exists():
            skipped.append({"label": "Oha-Yachiyo 应用配置", "reason": "备份中不存在"})
        elif not _is_restore_source_dir(app_config_source):
            skipped.append({
                "label": "Oha-Yachiyo 应用配置",
                "reason": "备份中的应用配置不是目录",
            })
        elif app_config_safe:
            try:
                _replace_path(app_config_source, app_config_target)
                restored.append({
                    "label": "Oha-Yachiyo 应用配置",
                    "path": str(app_config_target),
                })
            except Exception as exc:
                errors.append(f"应用配置导入失败：{exc}")
        else:
            skipped.append({"label": "Oha-Yachiyo 应用配置", "reason": app_config_reason})

        workspace_source = snapshot_dir / "oha-workspace"
        workspace_target = _oha_workspace_dir()
        workspace_safe, workspace_reason = is_safe_oha_workspace(workspace_target)
        if not workspace_source.exists():
            skipped.append({"label": "Oha-Yachiyo 工作空间", "reason": "备份中不存在"})
        elif not _is_restore_source_dir(workspace_source):
            skipped.append({
                "label": "Oha-Yachiyo 工作空间",
                "reason": "备份中的工作空间不是目录",
            })
        elif not _has_oha_workspace_marker(workspace_source):
            skipped.append({
                "label": "Oha-Yachiyo 工作空间",
                "reason": "备份中的工作空间缺少初始化标识",
            })
        elif workspace_safe:
            try:
                _replace_path(workspace_source, workspace_target)
                restored.append({
                    "label": "Oha-Yachiyo 工作空间",
                    "path": str(workspace_target),
                })
            except Exception as exc:
                errors.append(f"Oha-Yachiyo 工作空间导入失败：{exc}")
        else:
            skipped.append({"label": "Oha-Yachiyo 工作空间", "reason": workspace_reason})

    return BackupImportResult(
        ok=not errors,
        backup=backup,
        restored=restored,
        skipped=skipped,
        errors=errors,
    )
