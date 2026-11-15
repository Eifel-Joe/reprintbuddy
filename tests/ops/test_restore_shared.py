"""p531: the end of a restore, shared by the page and the command line (`ops/restore.py`).

What both ways do with it is pinned in tests/web/test_restore_both_ways.py; these are the pieces."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import dbtemplate
import pytest

from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops import restore
from reprintbuddy.ops.status import UpdateStatus, write_status


def test_the_backup_a_failed_rollback_names_is_protected(tmp_path):
    named = tmp_path / "backups" / "reprintbuddy-v1.0.0-20260101T080000Z.db"
    write_status(tmp_path, UpdateStatus(state="rollback_failed", backup=str(named), started_at=datetime.now(UTC)))
    assert restore.protected_backups(tmp_path) == [named]


def test_nothing_is_protected_after_any_other_end_of_an_update(tmp_path):
    assert restore.protected_backups(tmp_path) == []  # no update has run
    for state in ("succeeded", "failed", "rolled_back"):
        write_status(tmp_path, UpdateStatus(state=state, backup=str(tmp_path / "b.db"), started_at=datetime.now(UTC)))
        assert restore.protected_backups(tmp_path) == [], state


def test_a_failed_rollback_without_a_backup_protects_nothing(tmp_path):
    write_status(tmp_path, UpdateStatus(state="rollback_failed", backup=None, started_at=datetime.now(UTC)))
    assert restore.protected_backups(tmp_path) == []


# --- install: the one step that replaces the database -------------------------------------------------


NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def database(path: Path, queue_cap: int) -> Path:
    """A migrated database of this version with one value to tell it by, and one mark of the log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dbtemplate.copy_to(path)
    engine = make_engine(path)
    upgrade(engine)
    engine.dispose()
    con = sqlite3.connect(path)
    try:
        con.execute("UPDATE settings SET value = ? WHERE key = 'queue_cap'", (json.dumps(queue_cap),))
        if con.execute("SELECT changes()").fetchone()[0] == 0:
            con.execute("INSERT INTO settings (key, value) VALUES ('queue_cap', ?)", (json.dumps(queue_cap),))
        con.execute("DELETE FROM activity_marks")
        con.execute("INSERT INTO activity_marks (key, state, at) VALUES ('bambuddy', 'down', '2026-11-14 08:00:00')")
        con.commit()
    finally:
        con.close()
    return path


def queue_cap(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return json.loads(con.execute("SELECT value FROM settings WHERE key = 'queue_cap'").fetchone()[0])
    finally:
        con.close()


def test_install_replaces_the_database_and_keeps_what_the_log_needs(tmp_path):
    db_path = database(tmp_path / "data" / "reprintbuddy.db", 9)
    candidate = database(tmp_path / "work" / "candidate.db", 3)

    installed = restore.install(candidate, db_path, now=NOW)

    assert queue_cap(db_path) == 3
    assert installed.before is not None and queue_cap(installed.before) == 9
    assert "before-restore" in installed.before.name
    assert installed.marks == {"bambuddy": {"state": "down", "at": "2026-11-14T08:00:00"}}
    assert not candidate.exists()


def test_install_passes_the_protected_backups_to_the_pruning(tmp_path):
    db_path = database(tmp_path / "data" / "reprintbuddy.db", 9)
    folder = tmp_path / "data" / "backups"
    folder.mkdir()
    older = [folder / f"reprintbuddy-v1.0.{n}-2026010{n}T080000Z.db" for n in range(1, 7)]
    for path in older:
        path.write_bytes(b"old")

    restore.install(database(tmp_path / "work" / "candidate.db", 3), db_path, now=NOW, protect=[older[0]])

    assert older[0].exists() and not older[1].exists()


def test_without_leave_a_database_that_seems_unreadable_is_never_set_aside(tmp_path, monkeypatch):
    """The page restores into the database the running service holds open. A check that cannot read it
    (locked just then, say) must never lead to moving it away: the page never asks."""
    db_path = database(tmp_path / "data" / "reprintbuddy.db", 9)
    monkeypatch.setattr(restore, "database_readable", lambda path: False)

    installed = restore.install(database(tmp_path / "work" / "candidate.db", 3), db_path, now=NOW)

    assert queue_cap(db_path) == 3
    assert "before-restore" in installed.before.name
    assert list((tmp_path / "data" / "backups").glob("damaged-*")) == []


def test_without_leave_a_broken_database_is_refused_and_stays_where_it_is(tmp_path):
    db_path = tmp_path / "data" / "reprintbuddy.db"
    db_path.parent.mkdir()
    db_path.write_bytes(b"this is not a database" * 400)
    candidate = database(tmp_path / "work" / "candidate.db", 3)

    with pytest.raises(sqlite3.DatabaseError):
        restore.install(candidate, db_path, now=NOW)

    assert db_path.read_bytes().startswith(b"this is not a database")
    assert not (tmp_path / "data" / "backups").exists() or list((tmp_path / "data" / "backups").glob("damaged-*")) == []


def test_with_leave_a_broken_database_is_set_aside_and_replaced(tmp_path):
    db_path = tmp_path / "data" / "reprintbuddy.db"
    db_path.parent.mkdir()
    db_path.write_bytes(b"this is not a database" * 400)

    installed = restore.install(
        database(tmp_path / "work" / "candidate.db", 3), db_path, now=NOW, may_set_aside=True
    )

    assert queue_cap(db_path) == 3
    assert installed.before is not None and installed.before.name.startswith("damaged-")
    assert installed.before.read_bytes().startswith(b"this is not a database")
    assert installed.marks is None and installed.notify_marks is None


def test_with_leave_a_readable_database_is_replaced_as_without(tmp_path):
    db_path = database(tmp_path / "data" / "reprintbuddy.db", 9)

    installed = restore.install(
        database(tmp_path / "work" / "candidate.db", 3), db_path, now=NOW, may_set_aside=True
    )

    assert queue_cap(db_path) == 3
    assert "before-restore" in installed.before.name
    assert installed.marks == {"bambuddy": {"state": "down", "at": "2026-11-14T08:00:00"}}
