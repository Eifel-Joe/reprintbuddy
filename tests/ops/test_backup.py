import os
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops.backup import (
    KEEP_BACKUPS,
    backup_database,
    backup_dir_for,
    prune_backups,
    restore_database,
    update_backup_since,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data" / "r.db"
    path.parent.mkdir()
    engine = make_engine(path)
    upgrade(engine)
    maker = make_sessionmaker(engine)
    with maker() as session:
        st.set_setting(session, "queue_cap", 7)
    return path, engine, maker


def queue_cap(path):
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT value FROM settings WHERE key = 'queue_cap'").fetchone()
    finally:
        con.close()
    return None if row is None else int(row[0])


def test_backup_dir_for(tmp_path):
    assert backup_dir_for(tmp_path / "x" / "a.db") == tmp_path / "x" / "backups"
    assert KEEP_BACKUPS == 5


def test_backup_contains_committed_data(db):
    path, engine, maker = db
    holder = maker()  # a connection stays open, as in the running service
    holder.execute(text("SELECT 1"))
    with maker() as session:
        st.set_setting(session, "queue_cap", 7)
    backup = backup_database(path, backup_dir_for(path), label="x", now=NOW)
    holder.close()
    assert queue_cap(backup) == 7


def test_backup_name(db):
    path, *_ = db
    backup = backup_database(path, backup_dir_for(path), label="v0.1.0", now=NOW)
    assert backup == backup_dir_for(path) / "reprintbuddy-v0.1.0-20261105T120000Z.db"
    assert backup.exists()


def test_backup_label_sanitized(db):
    path, *_ = db
    backup = backup_database(path, backup_dir_for(path), label="a/b c", now=NOW)
    assert backup.name.startswith("reprintbuddy-a_b_c-")
    assert backup.parent == backup_dir_for(path)


def test_backup_missing_db(tmp_path):
    missing = tmp_path / "none.db"
    target = tmp_path / "backups"
    with pytest.raises(FileNotFoundError):
        backup_database(missing, target, label="x", now=NOW)
    assert not missing.exists()
    assert not target.exists() or list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX only")
def test_backup_modes(db):
    path, *_ = db
    backup = backup_database(path, backup_dir_for(path), label="x", now=NOW)
    assert (backup.stat().st_mode & 0o777) == 0o600
    assert (backup.parent.stat().st_mode & 0o777) == 0o700


def test_restore_replaces_content(db):
    path, engine, maker = db
    backup = backup_database(path, backup_dir_for(path), label="x", now=NOW)
    with maker() as session:
        st.set_setting(session, "queue_cap", 9)
    assert queue_cap(path) == 9
    restore_database(backup, path)
    with maker() as session:
        assert st.get_setting(session, "queue_cap") == 7


def test_restore_with_open_connection(db):
    path, engine, maker = db
    backup = backup_database(path, backup_dir_for(path), label="x", now=NOW)
    with maker() as session:
        st.set_setting(session, "queue_cap", 9)
    conn = engine.connect()
    conn.execute(text("SELECT 1"))
    restore_database(backup, path)
    value = conn.execute(text("SELECT value FROM settings WHERE key = 'queue_cap'")).scalar()
    conn.close()
    assert int(value) == 7


def test_restore_missing_backup(db, tmp_path):
    path, *_ = db
    with pytest.raises(FileNotFoundError):
        restore_database(tmp_path / "nope.db", path)
    assert queue_cap(path) == 7  # untouched


def test_prune_keeps_newest_five(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    names = [f"reprintbuddy-v0.1.{i}-2026110{i + 1}T120000Z.db" for i in range(7)]
    for name in names:
        (folder / name).write_bytes(b"x")
    (folder / "notes.txt").write_text("keep me")
    gone = prune_backups(folder)
    assert sorted(p.name for p in gone) == sorted(names[:2])
    left = sorted(p.name for p in folder.iterdir())
    assert left == sorted(names[2:] + ["notes.txt"])


def test_prune_sorts_by_timestamp_not_label(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    old = folder / "reprintbuddy-zzz-20250101T000000Z.db"
    new = folder / "reprintbuddy-aaa-20260101T000000Z.db"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    assert prune_backups(folder, keep=1) == [old]


def test_prune_never_deletes_a_protected_backup(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    old = folder / "reprintbuddy-v0.1.0-20250101T000000Z.db"
    old.write_bytes(b"x")
    for i in range(1, 6):
        (folder / f"reprintbuddy-v0.2.{i}-2026010{i}T000000Z.db").write_bytes(b"x")
    assert prune_backups(folder, protect=[old]) == []
    assert old.exists()
    assert len(list(folder.iterdir())) == 6  # the protected one lives on top of the newest five


# --- v0.3.12 p145: did the update helper back up the database since the start? -------------------------------

STARTED = datetime(2026, 11, 9, 10, 15, 30, 640000, tzinfo=UTC)


def named(folder: Path, name: str) -> None:
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(b"")


@pytest.mark.parametrize(
    "name, from_tag, expected",
    [
        ("reprintbuddy-v0.1.0-20261109T101530Z.db", "v0.1.0", True),  # the very second of the start
        ("reprintbuddy-v0.1.0-20261109T101645Z.db", "v0.1.0", True),
        ("reprintbuddy-v0.1.0-20261109T101529Z.db", "v0.1.0", False),  # one second before: an earlier one
        ("reprintbuddy-before-restore-20261109T101645Z.db", "v0.1.0", False),  # a restore's
        ("reprintbuddy-v0.0.9-20261109T101645Z.db", "v0.1.0", False),  # another installed version's label
        ("reprintbuddy-0123456789ab-20261109T101645Z.db", None, True),  # no tag: the commit's first 12
        ("reprintbuddy-before-restore-20261109T101645Z.db", None, False),
        ("notes-20261109T101645Z.db", "v0.1.0", False),  # a foreign file in the folder
        ("reprintbuddy-v0.1.0-20261109T101645Z.db-wal", "v0.1.0", False),
        ("reprintbuddy-v0.1.0-20261399T101645Z.db", "v0.1.0", False),  # no real time
    ],
)
def test_p145_update_backup_since(tmp_path, name, from_tag, expected):
    folder = tmp_path / "backups"
    named(folder, name)
    assert update_backup_since(folder, STARTED, from_tag) is expected


def test_p145_update_backup_since_compares_in_utc(tmp_path):
    folder = tmp_path / "backups"
    named(folder, "reprintbuddy-v0.1.0-20261109T101645Z.db")
    same_moment_elsewhere = STARTED.astimezone(timezone(timedelta(hours=2)))  # 12:15:30 at +02:00
    assert update_backup_since(folder, same_moment_elsewhere, "v0.1.0") is True
    assert update_backup_since(folder, same_moment_elsewhere + timedelta(minutes=2), "v0.1.0") is False


def test_p145_update_backup_since_without_a_folder(tmp_path):
    assert update_backup_since(tmp_path / "backups", STARTED, "v0.1.0") is False
