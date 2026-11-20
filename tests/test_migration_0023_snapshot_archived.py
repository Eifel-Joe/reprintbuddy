"""Revision 0023: the daily record marks the row of an archived spool (Spec 9.2, p717).

The step only adds the column `filament_snapshots.archived` (default false, no rebuilt table), so the way
back from the pre-release drops it again and leaves every row as it was. A backup of 1.2.1 restores and
migrates; its rows count as not archived.
"""

from datetime import UTC, datetime

from sqlalchemy import text

import pytest
from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops import fullbackup as fb

from test_migration_0015_part_suppliers import downgrade

BEFORE = "0022"
REVISION = "0023"

ROWS = "SELECT id, day, spool_id, spool_filament_id, remaining_g, label_g, spool_created_at FROM filament_snapshots ORDER BY id"


def fill_old(conn) -> None:
    conn.execute(text(
        "INSERT INTO filament_sorts (id, sort_key, material, label) VALUES (1, 'brand|petg||#000000', 'PETG', 'PETG')"
    ))
    conn.execute(text(
        "INSERT INTO filament_snapshots (id, day, spool_id, spool_filament_id, remaining_g, label_g)"
        " VALUES (1, '2026-11-19', 48, 1, 252.75, 1000), (2, '2026-11-19', 49, 1, 1000.0, 1000),"
        " (3, '2026-11-20', 49, 1, 720.19, 1000)"
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


def test_rows_of_1_2_1_count_as_not_archived(old_db):
    before = _rows(old_db, ROWS)
    upgrade(old_db, revision=REVISION)
    assert _rows(old_db, ROWS) == before
    assert _rows(old_db, "SELECT id, archived FROM filament_snapshots ORDER BY id") == [(1, 0), (2, 0), (3, 0)]


def test_downgrade_drops_the_column_and_keeps_every_row(old_db):
    before = _rows(old_db, ROWS)
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO filament_snapshots (id, day, spool_id, spool_filament_id, remaining_g, label_g, archived)"
            " VALUES (4, '2026-11-20', 48, 1, 83.59, 1000, 1)"
        ))
    downgrade(old_db, BEFORE)
    with old_db.connect() as conn:
        columns = [r[1] for r in conn.execute(text("PRAGMA table_info(filament_snapshots)"))]
    assert "archived" not in columns
    # the row of the archived spool stays a point of its use; 1.2.1 drops it itself if it is of today
    assert _rows(old_db, ROWS) == [*before, (4, "2026-11-20", 48, 1, 83.59, 1000, None)]


def test_full_backup_of_1_2_1_restores_and_migrates(tmp_path, old_db):
    old_db.dispose()
    work = tmp_path / "work"
    work.mkdir()
    created = fb.create_backup(tmp_path / "old.db", work_dir=work, password=None, now=datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
    manifest = fb.read_manifest(created.path)
    assert manifest.schema_revision == BEFORE
    restored = work / "candidate.db"
    fb.extract_database(created.path, manifest, None, restored)
    fb.verify_database(restored, manifest, work_dir=work)
    engine = make_engine(restored)
    upgrade(engine)
    assert _rows(engine, "SELECT id, archived FROM filament_snapshots ORDER BY id") == [(1, 0), (2, 0), (3, 0)]
    engine.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(restored) == fb._structure(tmp_path / "new.db")
