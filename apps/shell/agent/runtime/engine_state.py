"""Native runtime engine state and connection setup."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from apps.shell.agent.repositories.sqlite import (
    LockedConnection,
    named_row_factory,
    open_locked_runtime_connection,
)
from apps.shell.agent.runtime.budget import RunBudgetLimits
from apps.shell.agent.runtime.paths import runtime_directory_layout
from apps.shell.credential_store import CredentialStore, create_credential_store


@dataclass(frozen=True)
class RuntimeEngineStateBundle:
    workspace_dir: Path
    db_path: Path
    credential_store: CredentialStore
    skills_dir: Path
    skill_installs_dir: Path
    skill_installs_native_home: Path
    agent_artifacts_dir: Path
    workflow_artifacts_dir: Path
    agent_workspaces_dir: Path
    accepting_runs: bool
    closed: bool
    runtime_limits: RunBudgetLimits
    db_lock: threading.RLock
    approval_execution_lock: threading.RLock
    approval_execution_in_progress: set[str]
    run_cancel_locks: dict[str, threading.RLock]
    run_cancel_locks_guard: threading.RLock
    conn: LockedConnection


def build_runtime_engine_state(
    *,
    db_path: Path | str | None,
    workspace_dir: Path | str | None,
    credential_store: CredentialStore | None,
) -> RuntimeEngineStateBundle:
    layout = runtime_directory_layout(workspace_dir, db_path)
    db_lock = threading.RLock()
    conn = open_locked_runtime_connection(layout.db_path, db_lock)
    conn.row_factory = named_row_factory
    return RuntimeEngineStateBundle(
        workspace_dir=layout.root,
        db_path=layout.db_path,
        credential_store=credential_store or create_credential_store(layout.root),
        skills_dir=layout.skills_dir,
        skill_installs_dir=layout.skill_installs_dir,
        skill_installs_native_home=layout.skill_installs_native_home,
        agent_artifacts_dir=layout.agent_artifacts_dir,
        workflow_artifacts_dir=layout.workflow_artifacts_dir,
        agent_workspaces_dir=layout.agent_workspaces_dir,
        accepting_runs=True,
        closed=False,
        runtime_limits=RunBudgetLimits.from_environment(),
        db_lock=db_lock,
        approval_execution_lock=threading.RLock(),
        approval_execution_in_progress=set(),
        run_cancel_locks={},
        run_cancel_locks_guard=threading.RLock(),
        conn=conn,
    )
