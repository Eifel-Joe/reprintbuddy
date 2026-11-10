"""Revision 0016: several suppliers per spool sort, and "take back" of an order (Spec 9.3, 9.4).

The supplier fields of a sort's order settings move to `filament_suppliers`; every sort with
settings gets exactly one supplier, its default, with the values it had. A filament mark keeps the
roll size and the supplier of the moment it was set, as part marks do since 0015, so they are
copied onto the old filament marks too. `reorder_marks` learns whether a mark was taken back.
"""

import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text

from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops import fullbackup as fb

from test_migration_0015_part_suppliers import downgrade

BEFORE = "0015"
REVISION = "0016"
MOVED = {"lead_time_days", "pack_grams", "min_order_packs", "supplier", "order_url"}


def fill_old(conn) -> None:
    """Three spool sorts as v0.3.15 has them: two with settings (one without supplier text), one
    without; marks of filament (with and without settings, open and closed) and of a part."""
    conn.execute(text(
        "INSERT INTO filament_sorts (id, sort_key, legacy, brand, material, color_hex, label) VALUES"
        " (1, 'brand a|pla||#000000', 0, 'Brand A', 'PLA', '#000000', 'Brand A PLA Black'),"
        " (2, 'brand b|petg||#ffffff', 0, 'Brand B', 'PETG', '#FFFFFF', 'Brand B PETG White'),"
        " (3, 'brand c|pla||#ff0000', 0, 'Brand C', 'PLA', '#FF0000', 'Brand C PLA Red')"
    ))
    conn.execute(text(
        "INSERT INTO filament_settings (spool_filament_id, lead_time_days, safety_value, safety_unit,"
        " pack_grams, min_order_packs, supplier, order_url) VALUES"
        " (1, 3, 7, 'days', 1000, 2, 'Example Shop', 'https://shop.example/pla-black'),"
        " (2, 0, 500, 'grams', 750, 1, NULL, NULL)"
    ))
    conn.execute(text(
        "INSERT INTO parts (id, name, unit, stock, min_stock, safety_value, safety_unit) VALUES"
        " (1, 'Insert M4', 'pcs', 40, 10, 3, 'days')"
    ))
    conn.execute(text(
        "INSERT INTO part_suppliers (part_id, position, name, pack_size, min_order_packs, lead_time_days, is_default)"
        " VALUES (1, 0, 'Example Supplier', 50, 1, 7, 1)"
    ))
    conn.execute(text(
        "INSERT INTO reorder_marks (id, item_type, item_id, qty, marked_at, received_at, pack_size,"
        " supplier_name, supplier_position) VALUES"
        " (1, 'filament', 1, 3, '2026-11-01 08:00:00.000000', NULL, NULL, NULL, NULL),"
        " (2, 'filament', 2, 1, '2026-10-01 08:00:00.000000', '2026-10-05 08:00:00.000000', NULL, NULL, NULL),"
        " (3, 'filament', 3, 2, '2026-11-02 08:00:00.000000', NULL, NULL, NULL, NULL),"
        " (4, 'part', 1, 2, '2026-11-01 08:00:00.000000', NULL, 50, 'Example Supplier', 0)"
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
            "SELECT sort_id, position, name, order_url, pack_size, min_order_packs, lead_time_days, is_default"
            " FROM filament_suppliers ORDER BY sort_id, position"
        ))]


def test_every_sort_with_settings_gets_its_values_as_its_only_and_default_supplier(old_db):
    upgrade(old_db, revision=REVISION)
    assert suppliers(old_db) == [
        (1, 0, "Example Shop", "https://shop.example/pla-black", 1000, 2, 3, 1),
        (2, 0, None, None, 750, 1, 0, 1),
    ]


def test_the_settings_keep_the_safety_margin_and_lose_the_moved_fields(old_db):
    upgrade(old_db, revision=REVISION)
    columns = {c["name"] for c in inspect(old_db).get_columns("filament_settings")}
    assert columns == {"spool_filament_id", "safety_value", "safety_unit"}
    with old_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT spool_filament_id, safety_value, safety_unit FROM filament_settings ORDER BY spool_filament_id"
        )).all()
    assert [tuple(r) for r in rows] == [(1, 7, "days"), (2, 500, "grams")]


def test_filament_marks_keep_roll_size_and_supplier_of_their_time(old_db):
    """A sort without settings counted its mark in the default unit of 1000 g and has no supplier."""
    upgrade(old_db, revision=REVISION)
    with old_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, qty, pack_size, supplier_name, supplier_position, withdrawn FROM reorder_marks ORDER BY id"
        )).all()
    assert [tuple(r) for r in rows] == [
        (1, 3, 1000, "Example Shop", 0, 0),
        (2, 1, 750, None, 0, 0),
        (3, 2, 1000, None, None, 0),
        (4, 2, 50, "Example Supplier", 0, 0),
    ]


def test_one_default_per_sort_and_one_row_per_position(old_db):
    upgrade(old_db, revision=REVISION)
    insert = (
        "INSERT INTO filament_suppliers (sort_id, position, pack_size, min_order_packs, lead_time_days, is_default)"
        " VALUES (1, {position}, 1000, 1, 0, {default})"
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


@pytest.mark.parametrize(
    "column, value", [("pack_size", 0), ("min_order_packs", 0), ("lead_time_days", -1), ("position", -1)]
)
def test_the_numbers_keep_their_limits(old_db, column, value):
    upgrade(old_db, revision=REVISION)
    fields = {"position": 1, "pack_size": 1000, "min_order_packs": 1, "lead_time_days": 0, column: value}
    with old_db.connect() as conn, pytest.raises(Exception), conn.begin():
        conn.execute(text(
            "INSERT INTO filament_suppliers (sort_id, position, pack_size, min_order_packs, lead_time_days, is_default)"
            f" VALUES (1, {fields['position']}, {fields['pack_size']}, {fields['min_order_packs']},"
            f" {fields['lead_time_days']}, 0)"
        ))


def test_a_second_run_changes_nothing(old_db):
    upgrade(old_db, revision=REVISION)
    upgrade(old_db, revision=REVISION)
    assert len(suppliers(old_db)) == 2


def test_downgrade_brings_the_default_supplier_back_to_the_settings(old_db):
    """Other suppliers have no place in 0015; a mark taken back becomes a closed one."""
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        conn.execute(text(
            "INSERT INTO filament_suppliers (sort_id, position, name, pack_size, min_order_packs, lead_time_days,"
            " is_default) VALUES (1, 1, 'Second', 500, 1, 1, 0)"
        ))
        conn.execute(text("UPDATE filament_suppliers SET is_default = 0 WHERE sort_id = 1 AND position = 0"))
        conn.execute(text("UPDATE filament_suppliers SET is_default = 1 WHERE sort_id = 1 AND position = 1"))
        conn.execute(text("UPDATE reorder_marks SET withdrawn = 1, received_at = '2026-11-03 08:00:00' WHERE id = 3"))
    downgrade(old_db, BEFORE)
    assert "filament_suppliers" not in inspect(old_db).get_table_names()
    with old_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT spool_filament_id, lead_time_days, safety_value, safety_unit, pack_grams, min_order_packs,"
            " supplier, order_url FROM filament_settings ORDER BY spool_filament_id"
        )).all()
        assert [tuple(r) for r in rows] == [
            (1, 1, 7, "days", 500, 1, "Second", None), (2, 0, 500, "grams", 750, 1, None, None),
        ]
        marks = conn.execute(text(
            "SELECT id, received_at IS NOT NULL, pack_size, supplier_name FROM reorder_marks ORDER BY id"
        )).all()
        assert [tuple(r) for r in marks] == [
            (1, 0, None, None), (2, 1, None, None), (3, 1, None, None), (4, 0, 50, "Example Supplier"),
        ]
    assert "withdrawn" not in {c["name"] for c in inspect(old_db).get_columns("reorder_marks")}


def test_an_upgraded_database_has_the_structure_of_a_new_one(old_db, tmp_path):
    """The restore compares structures exactly (ops/fullbackup._structure); `filament_settings`
    is rebuilt here, so it must come out as in a new database."""
    upgrade(old_db)
    old_db.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(tmp_path / "old.db") == fb._structure(tmp_path / "new.db")


def test_a_backup_of_the_previous_version_restores_with_its_suppliers(old_db, tmp_path):
    """A full backup made by 0.3.15 (Spec 11.6): it is rebuilt at its own revision, the start
    migrates it; afterwards it looks like a new database and the values sit at the supplier."""
    old_db.dispose()
    work = tmp_path / "work"
    work.mkdir()
    created = fb.create_backup(
        tmp_path / "old.db", work_dir=work, password=None, now=datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    )
    manifest = fb.read_manifest(created.path)
    assert manifest.schema_revision == BEFORE
    target = work / "candidate.db"
    fb.extract_database(created.path, manifest, None, target)
    fb.verify_database(target, manifest, work_dir=work)
    restored = make_engine(target)
    upgrade(restored)  # what the start does after the restore
    assert suppliers(restored)[0] == (1, 0, "Example Shop", "https://shop.example/pla-black", 1000, 2, 3, 1)
    with restored.connect() as conn:
        assert conn.execute(text("SELECT pack_size, supplier_name FROM reorder_marks WHERE id = 1")).one() == (
            1000, "Example Shop",
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


def test_the_rebuilt_settings_table_reads_the_same_every_time(tmp_path):
    """The restore compares the SQL text with a new database; the rebuild is written out, not
    done with Alembic's batch mode, whose order of CHECKs changes from run to run (see 0015)."""
    texts = set()
    for i in range(8):
        engine = make_engine(tmp_path / f"run{i}.db")
        upgrade(engine)
        engine.dispose()
        con = sqlite3.connect(tmp_path / f"run{i}.db")
        try:
            texts.add(con.execute("SELECT sql FROM sqlite_master WHERE name = 'filament_settings'").fetchone()[0])
        finally:
            con.close()
    assert len(texts) == 1, texts
