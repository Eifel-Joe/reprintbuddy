"""Revision 0019: prints withdrawn after a cancel (Spec 7.2a, 0.4.1 p382).

A handed-over print can now end as `withdrawn` (Reprintbuddy deleted it from Bambuddy's queue) and
carries `withdraw_sent_at` while that delete is under way. `withdrawals` keeps, per article, how
many prints still have to come out of Bambuddy. Rows of earlier versions keep their values.
"""

import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops import fullbackup as fb

from test_migration_0015_part_suppliers import downgrade

BEFORE = "0018"
REVISION = "0019"

DISPATCH_COLUMNS = (
    "id, article_id, origin, bambuddy_item_id, status, units, enqueue_sent_at, queue_entry_id,"
    " queue_position, last_status, message, created_at, dispatched_at, finished_at, library_file_id, plate_id"
)
STATES = ("sending", "unclear", "queued", "printing", "completed", "failed", "cancelled", "missing", "returned")


def fill_old(conn) -> None:
    conn.execute(text(
        "INSERT INTO articles (id, name, library_file_id, plate_id, file_name, stock, reserved)"
        " VALUES (1, 'Segment', 1, 1, 'seg.3mf', 3, 0)"
    ))
    for i, state in enumerate(STATES, start=1):
        conn.execute(text(
            f"INSERT INTO dispatches ({DISPATCH_COLUMNS}) VALUES ({i}, 1, 'rule', {100 + i}, '{state}', 1,"
            " '2026-11-10 08:00:00', NULL, 0, 'pending', NULL, '2026-11-10 08:00:00', NULL, NULL, 1, 1)"
        ))


@pytest.fixture
def old_db(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision=BEFORE)
    with engine.begin() as conn:
        fill_old(conn)
    return engine


def _rows(engine, sql: str) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql)).all()]


def test_prints_keep_their_values(old_db):
    before = _rows(old_db, f"SELECT {DISPATCH_COLUMNS} FROM dispatches ORDER BY id")
    upgrade(old_db, revision=REVISION)
    assert _rows(old_db, f"SELECT {DISPATCH_COLUMNS} FROM dispatches ORDER BY id") == before
    assert _rows(old_db, "SELECT DISTINCT withdraw_sent_at FROM dispatches") == [(None,)]
    assert _rows(old_db, "SELECT version_num FROM alembic_version") == [(REVISION,)]


def test_withdrawn_is_a_state_and_others_are_still_refused(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        conn.execute(text(
            "UPDATE dispatches SET status = 'withdrawn', withdraw_sent_at = '2026-11-11 09:00:00' WHERE id = 3"
        ))
    with pytest.raises(IntegrityError):
        with old_db.begin() as conn:
            conn.execute(text("UPDATE dispatches SET status = 'gone' WHERE id = 3"))


def test_withdrawals_table(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        conn.execute(text("INSERT INTO orders (id, reference, order_date) VALUES (1, 'R-1', '2026-11-10')"))
        conn.execute(text("INSERT INTO withdrawals (article_id, prints, order_id) VALUES (1, 2, 1)"))
    assert _rows(old_db, "SELECT article_id, prints, order_id, refused FROM withdrawals") == [(1, 2, 1, None)]
    with pytest.raises(IntegrityError):
        with old_db.begin() as conn:
            conn.execute(text("UPDATE withdrawals SET prints = -1"))


def test_downgrade_turns_withdrawn_prints_into_missing(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        conn.execute(text("UPDATE dispatches SET status = 'withdrawn' WHERE id = 3"))
    downgrade(old_db, BEFORE)
    assert _rows(old_db, "SELECT status FROM dispatches WHERE id = 3") == [("missing",)]
    with old_db.connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
        columns = {r[1] for r in conn.execute(text("PRAGMA table_info(dispatches)"))}
    assert "withdrawals" not in tables and "withdraw_sent_at" not in columns


def test_the_rebuilt_dispatches_table_reads_the_same_every_time(tmp_path):
    """Written out, not Alembic's batch mode, whose order of CHECKs is not stable (see 0015, 0018)."""
    texts = set()
    for i in range(3):
        engine = make_engine(tmp_path / f"run{i}.db")
        upgrade(engine)
        engine.dispose()
        con = sqlite3.connect(tmp_path / f"run{i}.db")
        try:
            texts.add(con.execute("SELECT sql FROM sqlite_master WHERE name = 'dispatches'").fetchone()[0])
        finally:
            con.close()
    assert len(texts) == 1, texts


def _backup_and_restore(tmp_path, source, revision):
    work = tmp_path / "work"
    work.mkdir()
    created = fb.create_backup(source, work_dir=work, password=None, now=datetime(2026, 11, 11, 12, 0, tzinfo=UTC))
    manifest = fb.read_manifest(created.path)
    assert manifest.schema_revision == revision
    restored = work / "candidate.db"
    fb.extract_database(created.path, manifest, None, restored)
    fb.verify_database(restored, manifest, work_dir=work)
    return restored


def test_full_backup_of_0_4_0_restores_with_0_4_1(tmp_path, old_db):
    old_db.dispose()
    restored = _backup_and_restore(tmp_path, tmp_path / "old.db", BEFORE)
    engine = make_engine(restored)
    upgrade(engine)
    assert _rows(engine, "SELECT id, status FROM dispatches ORDER BY id") == list(enumerate(STATES, start=1))
    engine.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(restored) == fb._structure(tmp_path / "new.db")


def test_full_backup_with_a_pending_withdrawal_restores(tmp_path, old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        conn.execute(text("UPDATE dispatches SET status = 'withdrawn' WHERE id = 3"))
        conn.execute(text("UPDATE dispatches SET withdraw_sent_at = '2026-11-11 09:00:00' WHERE id = 4"))
        conn.execute(text("INSERT INTO withdrawals (article_id, prints, refused) VALUES (1, 1, NULL)"))
    old_db.dispose()
    restored = _backup_and_restore(tmp_path, tmp_path / "old.db", REVISION)
    engine = make_engine(restored)
    upgrade(engine)
    assert _rows(engine, "SELECT article_id, prints FROM withdrawals") == [(1, 1)]
    assert _rows(engine, "SELECT status, withdraw_sent_at IS NOT NULL FROM dispatches WHERE id IN (3, 4) ORDER BY id") == [
        ("withdrawn", 0), ("printing", 1),
    ]
    engine.dispose()
