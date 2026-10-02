"""Runtime SQLite schema initialization and migration orchestration."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from apps.shell.agent.runtime.credentials import agent_model_credential_ref
from apps.shell.credential_store import CredentialStoreError

logger = logging.getLogger(__name__)


_RUNTIME_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'custom',
    instructions TEXT NOT NULL DEFAULT '',
    persona_prompt TEXT NOT NULL DEFAULT '',
    model_mode TEXT NOT NULL DEFAULT 'profile',
    execution_backend TEXT NOT NULL DEFAULT 'native_profile',
    model_profile_id TEXT NOT NULL DEFAULT '',
    vision_model_profile_id TEXT NOT NULL DEFAULT '',
    model_provider TEXT NOT NULL DEFAULT 'openai_compatible',
    model_base_url TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    model_api_key TEXT NOT NULL DEFAULT '',
    model_credential_ref TEXT NOT NULL DEFAULT '',
    tool_policy_json TEXT NOT NULL DEFAULT '{}',
    workspace_policy_json TEXT NOT NULL DEFAULT '{}',
    skill_ids_json TEXT NOT NULL DEFAULT '[]',
    output_contract TEXT NOT NULL DEFAULT 'chat',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    local_path TEXT NOT NULL DEFAULT '',
    folder_id TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'local_dir',
    origin_path TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT NOT NULL DEFAULT '',
    sync_status TEXT NOT NULL DEFAULT 'imported',
    content_summary TEXT NOT NULL DEFAULT '',
    skill_markdown TEXT NOT NULL,
    asset_paths_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_folders (
    folder_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_scope TEXT NOT NULL DEFAULT 'all',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    nodes_json TEXT NOT NULL DEFAULT '[]',
    edges_json TEXT NOT NULL DEFAULT '[]',
    default_input_schema_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_deletions (
    item_type TEXT NOT NULL,
    item_key TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (item_type, item_key)
);
CREATE TABLE IF NOT EXISTS run_groups (
    run_group_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    workspace_dir TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    summary TEXT NOT NULL DEFAULT '',
    child_run_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    run_group_id TEXT NOT NULL DEFAULT '',
    client_request_id TEXT NOT NULL DEFAULT '',
    project_root_group INTEGER NOT NULL DEFAULT 0,
    async_lease_generation INTEGER NOT NULL DEFAULT 0,
    async_lease_owner_token TEXT NOT NULL DEFAULT '',
    async_lease_expires_at TEXT NOT NULL DEFAULT '',
    async_lease_heartbeat_at TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    runnable_id TEXT NOT NULL,
    status TEXT NOT NULL,
    user_goal TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    timeline_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    pending_approval_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_run_links (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL DEFAULT '',
    run_status TEXT NOT NULL DEFAULT '',
    last_event_sequence INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'native_runtime',
    visibility TEXT NOT NULL DEFAULT 'user',
    sensitivity TEXT NOT NULL DEFAULT 'public',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    UNIQUE (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS run_approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    tool TEXT NOT NULL DEFAULT '',
    input_preview_json TEXT NOT NULL DEFAULT '{}',
    payload_json TEXT NOT NULL DEFAULT '{}',
    requested_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS run_artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    source_run_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    UNIQUE (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS trusted_workspaces (
    path TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    trusted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_items (
    memory_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'global',
    kind TEXT NOT NULL DEFAULT 'fact',
    content TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    source_session_id TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_task_id TEXT NOT NULL DEFAULT '',
    source_run_id TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    pinned INTEGER NOT NULL DEFAULT 0,
    user_confirmed INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    actor TEXT NOT NULL DEFAULT 'agent_tool',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS memory_projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_project_sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES memory_projects(project_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS memory_events (
    event_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'agent_tool',
    payload_json TEXT NOT NULL DEFAULT '{}',
    source_run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_consent_capabilities (
    capability_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    memory_version TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    source_session_id TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    expires_at_epoch REAL NOT NULL,
    consumed_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (memory_id) REFERENCES memory_items(memory_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS future_tasks (
    future_task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    runnable_id TEXT NOT NULL DEFAULT '',
    runnable_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'scheduled',
    scheduled_at_epoch REAL NOT NULL,
    cron TEXT NOT NULL DEFAULT '',
    source_run_id TEXT NOT NULL DEFAULT '',
    last_run_id TEXT NOT NULL DEFAULT '',
    run_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT NOT NULL DEFAULT '',
    trigger_claim_id TEXT NOT NULL DEFAULT '',
    trigger_in_flight INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS future_task_events (
    event_id TEXT PRIMARY KEY,
    future_task_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'agent_runtime',
    payload_json TEXT NOT NULL DEFAULT '{}',
    source_run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_RUNTIME_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_agents_name ON agents (LOWER(name));
CREATE INDEX IF NOT EXISTS idx_workflows_name ON workflows (LOWER(name));
CREATE INDEX IF NOT EXISTS idx_skills_folder ON skills (folder_id);
CREATE INDEX IF NOT EXISTS idx_skills_origin ON skills (origin_path);
CREATE INDEX IF NOT EXISTS idx_skills_content_hash ON skills (content_hash);
CREATE INDEX IF NOT EXISTS idx_skill_folders_sort ON skill_folders (sort_order, LOWER(name));
CREATE INDEX IF NOT EXISTS idx_run_groups_status_updated ON run_groups (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_runs_group_updated ON runs (run_group_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_runs_kind_updated ON runs (kind, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_client_request ON runs (client_request_id) WHERE client_request_id != '';
CREATE INDEX IF NOT EXISTS idx_task_run_links_session ON task_run_links (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_events_run_sequence ON run_events (run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_run_approvals_run_status ON run_approvals (run_id, status);
CREATE INDEX IF NOT EXISTS idx_run_artifacts_run_sequence ON run_artifacts (run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_trusted_workspaces_updated ON trusted_workspaces (updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_items_scope_kind_updated ON memory_items (scope, kind, deleted_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_items_source_run ON memory_items (source_run_id);
CREATE INDEX IF NOT EXISTS idx_memory_project_sessions_project ON memory_project_sessions (project_id);
CREATE INDEX IF NOT EXISTS idx_memory_events_memory_created ON memory_events (memory_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_consent_memory_active ON memory_consent_capabilities (memory_id, consumed_at, expires_at_epoch);
CREATE INDEX IF NOT EXISTS idx_future_tasks_status_due ON future_tasks (status, scheduled_at_epoch);
CREATE INDEX IF NOT EXISTS idx_future_tasks_runnable_updated ON future_tasks (runnable_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_future_task_events_task_created ON future_task_events (future_task_id, created_at);
"""


class RuntimeSchemaMigrator:
    """Applies compatibility migrations after base Runtime tables exist."""

    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], str],
        redact_secrets: Callable[[Any], str],
        credential_store: Any,
    ) -> None:
        self._conn = conn
        self._now = now
        self._redact_secrets = redact_secrets
        self._credential_store = credential_store

    def ensure_runtime_columns(self) -> bool:
        columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(agents)").fetchall()}
        if "nickname" not in columns:
            self._conn.execute("ALTER TABLE agents ADD COLUMN nickname TEXT NOT NULL DEFAULT ''")
        if "persona_prompt" not in columns:
            self._conn.execute("ALTER TABLE agents ADD COLUMN persona_prompt TEXT NOT NULL DEFAULT ''")
        if "execution_backend" not in columns:
            self._conn.execute("ALTER TABLE agents ADD COLUMN execution_backend TEXT NOT NULL DEFAULT 'native_profile'")
        if "model_profile_id" not in columns:
            self._conn.execute("ALTER TABLE agents ADD COLUMN model_profile_id TEXT NOT NULL DEFAULT ''")
        if "vision_model_profile_id" not in columns:
            self._conn.execute("ALTER TABLE agents ADD COLUMN vision_model_profile_id TEXT NOT NULL DEFAULT ''")
        if "model_credential_ref" not in columns:
            self._conn.execute("ALTER TABLE agents ADD COLUMN model_credential_ref TEXT NOT NULL DEFAULT ''")
        skill_columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(skills)").fetchall()}
        if "local_path" not in skill_columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN local_path TEXT NOT NULL DEFAULT ''")
        if "folder_id" not in skill_columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN folder_id TEXT NOT NULL DEFAULT ''")
        if "enabled" not in skill_columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        if "source_type" not in skill_columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN source_type TEXT NOT NULL DEFAULT 'local_dir'")
        if "origin_path" not in skill_columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN origin_path TEXT NOT NULL DEFAULT ''")
        if "source_ref" not in skill_columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN source_ref TEXT NOT NULL DEFAULT ''")
        if "content_hash" not in skill_columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
        if "last_synced_at" not in skill_columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN last_synced_at TEXT NOT NULL DEFAULT ''")
        if "sync_status" not in skill_columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'imported'")
        run_columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "run_group_id" not in run_columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN run_group_id TEXT NOT NULL DEFAULT ''")
        if "client_request_id" not in run_columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN client_request_id TEXT NOT NULL DEFAULT ''")
        if "project_root_group" not in run_columns:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN project_root_group INTEGER NOT NULL DEFAULT 0"
            )
        if "async_lease_generation" not in run_columns:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN async_lease_generation INTEGER NOT NULL DEFAULT 0"
            )
        if "async_lease_owner_token" not in run_columns:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN async_lease_owner_token TEXT NOT NULL DEFAULT ''"
            )
        if "async_lease_expires_at" not in run_columns:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN async_lease_expires_at TEXT NOT NULL DEFAULT ''"
            )
        if "async_lease_heartbeat_at" not in run_columns:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN async_lease_heartbeat_at TEXT NOT NULL DEFAULT ''"
            )
        if "pending_approval_json" not in run_columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN pending_approval_json TEXT NOT NULL DEFAULT '{}'")
        future_task_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(future_tasks)").fetchall()
        }
        if future_task_columns and "trigger_claim_id" not in future_task_columns:
            self._conn.execute(
                "ALTER TABLE future_tasks ADD COLUMN trigger_claim_id TEXT NOT NULL DEFAULT ''"
            )
        if future_task_columns and "trigger_in_flight" not in future_task_columns:
            self._conn.execute(
                "ALTER TABLE future_tasks ADD COLUMN trigger_in_flight INTEGER NOT NULL DEFAULT 0"
            )
        memory_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(memory_items)").fetchall()
        }
        if memory_columns and "enabled" not in memory_columns:
            self._conn.execute(
                "ALTER TABLE memory_items ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        if memory_columns and "actor" not in memory_columns:
            self._conn.execute(
                "ALTER TABLE memory_items ADD COLUMN actor TEXT NOT NULL DEFAULT 'agent_tool'"
            )
        if memory_columns:
            self._conn.execute(
                """
                UPDATE memory_items
                   SET actor='user', user_confirmed=1
                 WHERE source_run_id='manual'
                """
            )
        task_run_link_columns = {
            str(row["name"]) for row in self._conn.execute("PRAGMA table_info(task_run_links)").fetchall()
        }
        if "run_status" not in task_run_link_columns:
            self._conn.execute("ALTER TABLE task_run_links ADD COLUMN run_status TEXT NOT NULL DEFAULT ''")
        if "last_event_sequence" not in task_run_link_columns:
            self._conn.execute(
                "ALTER TABLE task_run_links ADD COLUMN last_event_sequence INTEGER NOT NULL DEFAULT 0"
            )
        if "updated_at" not in task_run_link_columns:
            self._conn.execute("ALTER TABLE task_run_links ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        self._conn.execute(
            """
            UPDATE task_run_links
               SET run_status=COALESCE((SELECT status FROM runs WHERE runs.run_id=task_run_links.run_id), '')
             WHERE run_status=''
            """
        )
        self._conn.execute(
            """
            UPDATE task_run_links
               SET last_event_sequence=COALESCE(
                    (SELECT MAX(sequence) FROM run_events WHERE run_events.run_id=task_run_links.run_id),
                    0
               )
             WHERE last_event_sequence=0
            """
        )
        self._conn.execute(
            """
            UPDATE task_run_links
               SET updated_at=created_at
             WHERE updated_at=''
            """
        )
        self.migrate_native_execution_and_skill_sources()
        scrubbed_run_groups = self.migrate_run_group_secret_projections()
        scrubbed_agent_credentials = self.migrate_agent_model_credentials()
        return scrubbed_run_groups or scrubbed_agent_credentials

    def migrate_native_execution_and_skill_sources(self) -> None:
        self._conn.execute(
            """
            UPDATE agents
               SET execution_backend='native_profile'
             WHERE execution_backend IN ('yachiyo_profile', 'external_cli', '')
            """
        )
        self._conn.execute(
            """
            UPDATE skill_folders
               SET source_scope='installed'
             WHERE source_scope='yachiyo'
            """
        )
        self._conn.execute(
            """
            UPDATE studio_deletions
               SET item_key='installed:' || substr(item_key, 9)
             WHERE item_type='skill_source'
               AND item_key LIKE 'yachiyo:%'
            """
        )

    def migrate_run_group_secret_projections(self) -> bool:
        scrubbed = False
        rows = self._conn.execute(
            "SELECT run_group_id, title, source, workspace_dir, summary FROM run_groups"
        ).fetchall()
        for row in rows:
            clean_title = self._redact_secrets(row["title"])[:180]
            clean_source = self._redact_secrets(row["source"])[:80]
            clean_workspace_dir = self._redact_secrets(row["workspace_dir"])
            clean_summary = self._redact_secrets(row["summary"])
            if (
                clean_title == row["title"]
                and clean_source == row["source"]
                and clean_workspace_dir == row["workspace_dir"]
                and clean_summary == row["summary"]
            ):
                continue
            self._conn.execute(
                """
                UPDATE run_groups
                   SET title=?, source=?, workspace_dir=?, summary=?, updated_at=?
                 WHERE run_group_id=?
                """,
                (
                    clean_title,
                    clean_source,
                    clean_workspace_dir,
                    clean_summary,
                    self._now(),
                    str(row["run_group_id"]),
                ),
            )
            scrubbed = True
        return scrubbed

    def migrate_agent_model_credentials(self) -> bool:
        scrubbed = False
        rows = self._conn.execute(
            "SELECT agent_id, model_api_key, model_credential_ref FROM agents WHERE model_api_key<>''"
        ).fetchall()
        for row in rows:
            secret = str(row["model_api_key"] or "").strip()
            if not secret:
                continue
            credential_ref = (
                str(row["model_credential_ref"] or "").strip()
                or agent_model_credential_ref(str(row["agent_id"]))
            )
            try:
                self._credential_store.set(credential_ref, secret)
            except CredentialStoreError:
                continue
            self._conn.execute(
                "UPDATE agents SET model_credential_ref=?, model_api_key='' WHERE agent_id=?",
                (credential_ref, str(row["agent_id"])),
            )
            scrubbed = True
        return scrubbed


class RuntimeSchemaService:
    """Coordinates runtime schema initialization and compatibility migrations."""

    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], str],
        redact_secrets: Callable[[Any], str],
        credential_store: Any,
    ) -> None:
        self._conn = conn
        self._now = now
        self._redact_secrets = redact_secrets
        self._credential_store = credential_store

    def init_db(self) -> None:
        initialize_runtime_schema(
            self._conn,
            now=self._now,
            ensure_runtime_columns=self.ensure_runtime_columns,
            vacuum_after_secret_scrub=self.vacuum_after_secret_scrub,
        )

    def migrator(self) -> RuntimeSchemaMigrator:
        return RuntimeSchemaMigrator(
            self._conn,
            now=self._now,
            redact_secrets=self._redact_secrets,
            credential_store=self._credential_store,
        )

    def ensure_runtime_columns(self) -> bool:
        return self.migrator().ensure_runtime_columns()

    def vacuum_after_secret_scrub(self) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("VACUUM")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            logger.debug("Runtime schema secret scrub vacuum failed", exc_info=True)

    def migrate_native_execution_and_skill_sources(self) -> None:
        self.migrator().migrate_native_execution_and_skill_sources()

    def migrate_run_group_secret_projections(self) -> bool:
        return self.migrator().migrate_run_group_secret_projections()

    def migrate_agent_model_credentials(self) -> bool:
        return self.migrator().migrate_agent_model_credentials()


def initialize_runtime_schema(
    conn: Any,
    *,
    now: Callable[[], str],
    ensure_runtime_columns: Callable[[], bool],
    vacuum_after_secret_scrub: Callable[[], None],
) -> None:
    conn.executescript(_RUNTIME_TABLE_SCHEMA)
    scrubbed_secrets = ensure_runtime_columns()
    conn.executescript(_RUNTIME_INDEX_SCHEMA)
    conn.execute(
        """
        INSERT INTO runtime_schema_metadata (key, value, updated_at)
        VALUES ('schema_version', '1', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (now(),),
    )
    conn.commit()
    if scrubbed_secrets:
        vacuum_after_secret_scrub()
