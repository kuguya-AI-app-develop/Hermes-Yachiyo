"""Foreground desktop action locking for multi-agent runs."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ForegroundActionLease:
    acquired: bool
    holder: str = ""
    tool_name: str = ""
    locked_by: str = ""
    _release: Callable[[], None] | None = field(default=None, repr=False)

    def release(self) -> None:
        release = self._release
        self._release = None
        if release is not None:
            release()


class ForegroundActionLock:
    """Non-blocking lock for foreground click/type/hotkey actions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner = ""

    @property
    def owner(self) -> str:
        return self._owner

    def acquire(self, *, holder: str, tool_name: str) -> ForegroundActionLease:
        clean_holder = str(holder or "").strip() or "foreground-action"
        clean_tool_name = str(tool_name or "").strip()
        if not self._lock.acquire(blocking=False):
            return ForegroundActionLease(
                acquired=False,
                holder=clean_holder,
                tool_name=clean_tool_name,
                locked_by=self._owner,
            )
        self._owner = clean_holder
        return ForegroundActionLease(
            acquired=True,
            holder=clean_holder,
            tool_name=clean_tool_name,
            _release=lambda: self._release(clean_holder),
        )

    def _release(self, holder: str) -> None:
        if self._owner == holder:
            self._owner = ""
        self._lock.release()


_NATIVE_FOREGROUND_ACTION_LOCK = ForegroundActionLock()


def shared_native_foreground_lock() -> ForegroundActionLock:
    """Serialize controlled Native inputs across factories in this process.

    This does not prevent physical user input or actions in other processes.
    """
    return _NATIVE_FOREGROUND_ACTION_LOCK


__all__ = ["ForegroundActionLease", "ForegroundActionLock", "shared_native_foreground_lock"]
