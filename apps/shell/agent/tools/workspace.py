"""Workspace path and patch helpers for controlled Agent tools."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from apps.shell.agent.runtime.errors import AgentRuntimeError, AgentWorkspaceBoundaryError

_UNIFIED_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def _safe_rel_path(value: str) -> str:
    candidate = str(value or "").replace("\\", "/").strip()
    if (
        not candidate
        or candidate.startswith("/")
        or candidate.startswith("../")
        or "/../" in candidate
    ):
        raise AgentWorkspaceBoundaryError("路径必须是相对路径，且不能越界")
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def _read_text(path: Path, limit: int = 200_000) -> str:
    data = path.read_bytes()[:limit]
    return data.decode("utf-8", errors="replace")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _atomic_write_text(target: Path, content: str) -> None:
    tmp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _open_directory_nofollow(path: Path, *, create: bool = False) -> int:
    """Pin each component, never following a changed ancestor symlink."""
    if not path.is_absolute():
        raise OSError("workspace_create_parent_must_be_absolute")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("workspace_secure_create_unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _assert_directory_namespace(path: Path, pinned: int) -> None:
    current = _open_directory_nofollow(path)
    try:
        if not _same_inode(os.fstat(current), os.fstat(pinned)):
            raise OSError("workspace_create_parent_changed")
    finally:
        os.close(current)


def _atomic_create_text(target: Path, content: str) -> bytes:
    """Exclusively publish and independently read one file in a pinned parent."""
    parent = _open_directory_nofollow(target.parent, create=True)
    temp_name = f".{target.name}.{uuid4().hex}.tmp"
    temp_fd = -1
    try:
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        data = content.encode("utf-8")
        with os.fdopen(os.dup(temp_fd), "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        created = os.fstat(temp_fd)
        _assert_directory_namespace(target.parent, parent)
        if not _same_inode(created, os.stat(temp_name, dir_fd=parent, follow_symlinks=False)):
            raise OSError("workspace_create_staging_file_changed")
        os.link(
            temp_name,
            target.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
            follow_symlinks=False,
        )
        read_fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            before = os.fstat(read_fd)
            if not stat.S_ISREG(before.st_mode) or not _same_inode(before, created):
                raise OSError("workspace_create_target_changed")
            if before.st_size != len(data):
                raise OSError("workspace_create_size_changed")
            observed = bytearray()
            while len(observed) <= len(data):
                chunk = os.read(read_fd, min(65536, len(data) + 1 - len(observed)))
                if not chunk:
                    break
                observed.extend(chunk)
            after = os.fstat(read_fd)
            if (
                not _same_inode(before, after)
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or len(observed) != len(data)
                or not _same_inode(
                    created, os.stat(target.name, dir_fd=parent, follow_symlinks=False)
                )
            ):
                raise OSError("workspace_create_readback_changed")
            _assert_directory_namespace(target.parent, parent)
            return bytes(observed)
        finally:
            os.close(read_fd)
    finally:
        try:
            if temp_fd >= 0:
                try:
                    if _same_inode(
                        os.fstat(temp_fd), os.stat(temp_name, dir_fd=parent, follow_symlinks=False)
                    ):
                        os.unlink(temp_name, dir_fd=parent)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(temp_fd)
        finally:
            os.close(parent)


def _normalize_unified_diff_path(value: str) -> str:
    path = str(value or "").strip()
    if "\t" in path:
        path = path.split("\t", 1)[0].strip()
    elif " " in path:
        path = path.split(" ", 1)[0].strip()
    if path in {"", "/dev/null"}:
        raise AgentRuntimeError("workspace.write_patch 不支持删除或创建型 /dev/null patch")
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return _safe_rel_path(path)


def _apply_single_file_unified_diff(
    original: str, patch: str, *, expected_path: str, allow_create: bool = False
) -> str:
    if not patch.strip():
        raise AgentRuntimeError("workspace.write_patch patch 不能为空")
    if any(marker in patch for marker in ("GIT binary patch", "Binary files ")):
        raise AgentRuntimeError("workspace.write_patch 不支持二进制 patch")
    if re.search(r"(?m)^(rename from|rename to|deleted file mode|new file mode)\b", patch):
        raise AgentRuntimeError("workspace.write_patch 不支持重命名、删除或新文件 patch")

    lines = patch.splitlines(keepends=True)
    old_headers = [line for line in lines if line.startswith("--- ")]
    new_headers = [line for line in lines if line.startswith("+++ ")]
    if len(old_headers) != 1 or len(new_headers) != 1:
        raise AgentRuntimeError("workspace.write_patch 只支持单文件 unified diff")
    creating = allow_create and old_headers[0][4:].rstrip("\r\n") == "/dev/null"
    if creating and original:
        raise AgentRuntimeError("workspace.write_patch create patch 要求空的原始内容")
    old_path = None if creating else _normalize_unified_diff_path(old_headers[0][4:].rstrip("\r\n"))
    new_path = _normalize_unified_diff_path(new_headers[0][4:].rstrip("\r\n"))
    clean_expected_path = _safe_rel_path(expected_path)
    if (not creating and old_path != clean_expected_path) or new_path != clean_expected_path:
        raise AgentRuntimeError("workspace.write_patch patch 路径必须与目标 path 一致")

    original_lines = original.splitlines(keepends=True)
    output: list[str] = []
    old_pos = 0
    index = 0
    hunk_count = 0
    while index < len(lines):
        line = lines[index]
        match = _UNIFIED_HUNK_RE.match(line)
        if match is None:
            index += 1
            continue

        hunk_count += 1
        old_start = int(match.group("old_start"))
        old_count = int(match.group("old_count") or "1")
        new_count = int(match.group("new_count") or "1")
        if creating and (
            hunk_count != 1
            or old_start != 0
            or old_count != 0
            or int(match.group("new_start")) != 1
            or new_count <= 0
        ):
            raise AgentRuntimeError(
                "workspace.write_patch create patch 只允许单个 zero-old-line hunk"
            )
        hunk_old_pos = max(0, old_start - 1)
        if hunk_old_pos < old_pos:
            raise AgentRuntimeError("workspace.write_patch hunk 顺序无效")
        output.extend(original_lines[old_pos:hunk_old_pos])
        old_pos = hunk_old_pos
        consumed_old = 0
        produced_new = 0
        index += 1
        while index < len(lines) and not _UNIFIED_HUNK_RE.match(lines[index]):
            hunk_line = lines[index]
            if hunk_line.startswith(("--- ", "+++ ")):
                raise AgentRuntimeError("workspace.write_patch 不支持多文件 patch")
            if hunk_line.startswith("\\"):
                index += 1
                continue
            if not hunk_line:
                raise AgentRuntimeError("workspace.write_patch hunk 格式无效")
            marker = hunk_line[0]
            text = hunk_line[1:]
            if marker in {" ", "-"}:
                if old_pos >= len(original_lines) or original_lines[old_pos] != text:
                    raise AgentRuntimeError("workspace.write_patch hunk context 与当前文件不匹配")
                consumed_old += 1
                old_pos += 1
                if marker == " ":
                    output.append(text)
                    produced_new += 1
            elif marker == "+":
                output.append(text)
                produced_new += 1
            else:
                raise AgentRuntimeError("workspace.write_patch hunk 行格式无效")
            index += 1
        if consumed_old != old_count or produced_new != new_count:
            raise AgentRuntimeError("workspace.write_patch hunk 行数与 header 不一致")

    if hunk_count == 0:
        raise AgentRuntimeError("workspace.write_patch 缺少 unified diff hunk")
    output.extend(original_lines[old_pos:])
    return "".join(output)
