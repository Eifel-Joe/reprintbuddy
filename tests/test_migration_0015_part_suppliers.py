"""Revision 0015: several suppliers per purchased part (Spec 5.3, 9.3, 9.4).

The supplier fields of a part move to `part_suppliers`; every existing part gets exactly one
supplier, its default, with the values it had. A mark "ordered" of a part keeps the pack size and
the supplier name of the moment it was set (Spec 9.4), so they are copied onto the old marks too.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from reprintbuddy import db as dbmod
from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops import fullbackup as fb

BEFORE = "0013"
REVISION = "0015"
MOVED = {"pack_size", "min_order_packs", "lead_time_days", "supplier", "order_url"}


def downgrade(engine, revision: str) -> None:
    """The mirror of `db.upgrade`, as in the test of revision 0011."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(dbmod.__file__).parent / "migrations"))
    migration_engine = create_engine(
        engine.url, future=True, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
    dbmod._configure(migration_engine, foreign_keys=False, transactional_ddl=True)
    try:
        with migration_engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, revision)
    finally:
        migration_engine.dispose()


def fill_old(conn) -> None:
    """Two parts as v0.3.14 has them, one without supplier text; an article using one; marks."""
    conn.execute(text(
        "INSERT INTO parts (id, name, unit, stock, min_stock, pack_size, min_order_packs, lead_time_days,"
        " safety_value, safety_unit, supplier, order_url) VALUES"
        " (1, 'Insert M4', 'pcs', 40, 10, 50, 2, 7, 3, 'days', 'Example Supplier', 'https://supplier.example/a'),"
        " (2, 'Rubber foot', 'pcs', 5, 0, 1, 1, 0, 0, 'qty', NULL, NULL)"
    ))
    conn.execute(text(
        "INSERT INTO articles (id, name, library_file_id, plate_id, file_name) VALUES (1, 'Bracket', 1, 1, 'b.3mf')"
    ))
    conn.execute(text("INSERT INTO article_parts (article_id, part_id, qty_per_unit) VALUES (1, 1, 2)"))
    conn.execute(text(
        "INSERT INTO reorder_marks (id, item_type, item_id, qty, marked_at, received_at) VALUES"
        " (1, 'part', 1, 3, '2026-11-01 08:00:00.000000', NULL),"
        " (2, 'part', 2, 1, '2026-10-01 08:00:00.000000', '2026-10-05 08:00:00.000000'),"
        " (3, 'filament', 9, 2, '2026-11-01 08:00:00.000000', NULL)"
    ))


@pytest.fixture
def old_db(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision=BEFORE)
    with engine.begin() as conn:
        fill_old(conn)
    return engine


def suppliers(engine) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT part_id, position, name, order_url, pack_size, min_order_packs, lead_time_days, is_default"
            " FROM part_suppliers ORDER BY part_id, position"
        ))]


def test_every_part_gets_its_values_as_its_only_and_default_supplier(old_db):
    upgrade(old_db, revision=REVISION)
    assert suppliers(old_db) == [
        (1, 0, "Example Supplier", "https://supplier.example/a", 50, 2, 7, 1),
        (2, 0, None, None, 1, 1, 0, 1),
    ]


def test_the_part_keeps_its_own_fields_and_loses_the_moved_ones(old_db):
    upgrade(old_db, revision=REVISION)
    columns = {c["name"] for c in inspect(old_db).get_columns("parts")}
    assert not columns & MOVED
    with old_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, name, unit, stock, min_stock, safety_value, safety_unit FROM parts ORDER BY id"
        )).all()
        assert [tuple(r) for r in rows] == [
            (1, "Insert M4", "pcs", 40, 10, 3, "days"), (2, "Rubber foot", "pcs", 5, 0, 0, "qty"),
        ]
        assert conn.execute(text("SELECT article_id, part_id, qty_per_unit FROM article_parts")).all() == [(1, 1, 2)]


def test_part_marks_keep_pack_size_and_supplier_of_their_time(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, qty, pack_size, supplier_name, supplier_position FROM reorder_marks ORDER BY id"
        )).all()
    assert [tuple(r) for r in rows] == [
        (1, 3, 50, "Example Supplier", 0), (2, 1, 1, None, 0), (3, 2, None, None, None),
    ]


def test_one_default_per_part_and_one_row_per_position(old_db):
    upgrade(old_db, revision=REVISION)
    insert = (
        "INSERT INTO part_suppliers (part_id, position, pack_size, min_order_packs, lead_time_days, is_default)"
        " VALUES (1, {position}, 1, 1, 0, {default})"
    )
    with old_db.connect() as conn:
        with pytest.raises(Exception):
            with conn.begin():
                conn.execute(text(insert.format(position=1, default=1)))
        with pytest.raises(Exception):
            with conn.begin():
                conn.execute(text(insert.format(position=0, default=0)))
        with conn.begin():
            conn.execute(text(insert.format(position=1, default=0)))
    assert len(suppliers(old_db)) == 3


@pytest.mark.parametrize("column, value", [("pack_size", 0), ("min_order_packs", 0), ("lead_time_days", -1)])
def test_the_numbers_keep_their_limits(old_db, column, value):
    upgrade(old_db, revision=REVISION)
    fields = {"pack_size": 1, "min_order_packs": 1, "lead_time_days": 0, column: value}
    with old_db.connect() as conn, pytest.raises(Exception), conn.begin():
        conn.execute(text(
            "INSERT INTO part_suppliers (part_id, position, pack_size, min_order_packs, lead_time_days, is_default)"
            f" VALUES (1, 1, {fields['pack_size']}, {fields['min_order_packs']}, {fields['lead_time_days']}, 0)"
        ))


def test_a_second_run_changes_nothing(old_db):
    upgrade(old_db, revision=REVISION)
    upgrade(old_db, revision=REVISION)
    assert len(suppliers(old_db)) == 2


def test_downgrade_brings_the_default_supplier_back_to_the_part(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:  # a second supplier, not the default: it has no place in 0013
        conn.execute(text(
            "INSERT INTO part_suppliers (part_id, position, name, pack_size, min_order_packs, lead_time_days, is_default)"
            " VALUES (1, 1, 'Second', 10, 1, 1, 0)"
        ))
        conn.execute(text("UPDATE part_suppliers SET is_default = 0 WHERE part_id = 1 AND position = 0"))
        conn.execute(text("UPDATE part_suppliers SET is_default = 1 WHERE part_id = 1 AND position = 1"))
    downgrade(old_db, BEFORE)
    assert "part_suppliers" not in inspect(old_db).get_table_names()
    with old_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, pack_size, min_order_packs, lead_time_days, supplier, order_url FROM parts ORDER BY id"
        )).all()
        assert [tuple(r) for r in rows] == [(1, 10, 1, 1, "Second", None), (2, 1, 1, 0, None, None)]
        marks = {c["name"] for c in inspect(old_db).get_columns("reorder_marks")}
        assert not marks & {"pack_size", "supplier_name", "supplier_position"}


def test_an_upgraded_database_has_the_structure_of_a_new_one(old_db, tmp_path):
    """The restore compares structures exactly (ops/fullbackup._structure); `parts` is rebuilt
    here, so the order of every foreign key that points at it must come out as in a new one."""
    upgrade(old_db)
    old_db.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(tmp_path / "old.db") == fb._structure(tmp_path / "new.db")


def test_a_backup_of_the_previous_version_restores_with_its_suppliers(old_db, tmp_path):
    """A full backup made before 0015 (Spec 11.6): it is rebuilt at its own revision, the start
    migrates it; afterwards it looks like a new database and the values sit at the supplier."""
    old_db.dispose()
    work = tmp_path / "work"
    work.mkdir()
    created = fb.create_backup(tmp_path / "old.db", work_dir=work, password=None, now=datetime(2026, 11, 9, 12, 0, tzinfo=UTC))
    manifest = fb.read_manifest(created.path)
    assert manifest.schema_revision == BEFORE
    target = work / "candidate.db"
    fb.extract_database(created.path, manifest, None, target)
    fb.verify_database(target, manifest, work_dir=work)
    restored = make_engine(target)
    upgrade(restored)  # what the start does after the restore
    assert suppliers(restored)[0] == (1, 0, "Example Supplier", "https://supplier.example/a", 50, 2, 7, 1)
    with restored.connect() as conn:
        assert conn.execute(text("SELECT pack_size, supplier_name FROM reorder_marks WHERE id = 1")).one() == (
            50, "Example Supplier",
        )
    restored.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(target) == fb._structure(tmp_path / "new.db")
    con = sqlite3.connect(target)
    try:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


def test_the_rebuilt_parts_table_reads_the_same_every_time(tmp_path):
    """The restore rebuilds a backup from the migrations and compares the SQL text with a new
    database (test_fullbackup: test_restored_schema_is_built_from_the_migrations). Alembic's batch
    rebuild takes the CHECKs of a table from a set, so their order changed from run to run; the
    rebuild of `parts` is written out instead."""
    texts = set()
    for i in range(8):
        engine = make_engine(tmp_path / f"run{i}.db")
        upgrade(engine)
        engine.dispose()
        con = sqlite3.connect(tmp_path / f"run{i}.db")
        try:
            texts.add(con.execute("SELECT sql FROM sqlite_master WHERE name = 'parts'").fetchone()[0])
        finally:
            con.close()
    assert len(texts) == 1, texts
