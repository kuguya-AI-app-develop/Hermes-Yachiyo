"""SQLite backups must remain readable across concurrent WAL checkpoints."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from apps.installer import backup


@pytest.mark.parametrize("name", ["chat.db", "activity.db", "agent-runtime.db", "model-profiles.db"])
def test_backup_snapshots_each_runtime_database_across_wal_checkpoint(tmp_path, monkeypatch, name):
    source = tmp_path / "workspace"
    source.mkdir()
    db = source / name
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE important_data (value TEXT NOT NULL)")
        conn.execute("INSERT INTO important_data VALUES ('committed before backup')")
        conn.commit()
        assert Path(str(db) + "-wal").stat().st_size > 0
        original_copy2 = backup.shutil.copy2

        def copy_then_checkpoint(src, dest, **kwargs):
            result = original_copy2(src, dest, **kwargs)
            if Path(src) == db:
                # The application can checkpoint after the main file is copied
                # but before copytree reaches its WAL sidecar.
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return result

        monkeypatch.setattr(backup.shutil, "copy2", copy_then_checkpoint)
        destination = tmp_path / "snapshot"
        destination.mkdir()
        # copytree does not specify filesystem iteration order. Reproduce the
        # valid main-file-first ordering using its production copy callbacks.
        names = [name, f"{name}-wal", f"{name}-shm"]
        ignored = backup._copy_ignore(str(source), names)
        for entry in names:
            if entry not in ignored:
                backup._copy_file(str(source / entry), str(destination / entry))

        with closing(sqlite3.connect(destination / name)) as restored:
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert restored.execute("SELECT value FROM important_data").fetchall() == [
                ("committed before backup",)
            ]
        assert not (destination / f"{name}-wal").exists()
        assert not (destination / f"{name}-shm").exists()
    finally:
        conn.close()
