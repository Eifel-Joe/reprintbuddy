"""Revision 0006: sorts of one material, subtype and similar colour become one (Spec 9.1)."""

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from reprintbuddy import db as dbmod
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.models import JournalEntry


@pytest.fixture
def old_db(tmp_path):
    """A database at revision 0005 (sorts with brands in the key) and the full scripts for the step to 0006."""
    scripts = tmp_path / "migrations"
    shutil.copytree(Path(dbmod.__file__).parent / "migrations", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    for later in (scripts / "versions").glob("0*.py"):
        if later.name[:4] >= "0006":  # 0006 and every revision after it, whatever number is the newest
            later.unlink()
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, script_location=scripts)
    return engine


def add_sort(conn, sid, brand, material, hex_value, *, subtype=None, name="Black"):
    key = "|".join(x.strip().casefold() for x in (brand, material, subtype or "", hex_value or ""))
    conn.execute(
        text("INSERT INTO filament_sorts (id, sort_key, brand, material, subtype, color_hex, color_name, label)"
             " VALUES (:i, :k, :b, :m, :s, :h, :n, :l)"),
        {"i": sid, "k": key, "b": brand, "m": material, "s": subtype, "h": hex_value, "n": name,
         "l": f"{brand} {material} {name}"},
    )


def add_settings(conn, sid, **fields):
    values = {"lead_time_days": 5, "safety_value": 300, "safety_unit": "grams", "pack_grams": 1000,
              "min_order_packs": 1, "supplier": "Example Supplier", "order_url": None, **fields}
    conn.execute(
        text("INSERT INTO filament_settings VALUES (:i, :lead_time_days, :safety_value, :safety_unit,"
             " :pack_grams, :min_order_packs, :supplier, :order_url)"),
        {"i": sid, **values},
    )


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def test_two_brands_of_one_colour_become_one_sort_and_everything_follows(old_db):
    with old_db.begin() as conn:
        add_sort(conn, 1, "Brand One", "PETG", "#000000", name="Black")
        add_sort(conn, 2, "Brand Two", "PETG", "#1A1A1A", name="Schwarz")
        add_sort(conn, 3, "Brand Two", "PETG", "#FF0000", name="Red")
        conn.execute(text("INSERT INTO articles (id, name, library_file_id, plate_id, file_name) VALUES (1, 'Template A', 1, 1, 'a.gcode.3mf')"))
        conn.execute(text("INSERT INTO article_filaments (article_id, slot, grams, spool_filament_id) VALUES (1, 1, 10, 2)"))
        conn.execute(text("INSERT INTO article_filaments (article_id, slot, grams, spool_filament_id) VALUES (1, 2, 10, 3)"))
        conn.execute(text("INSERT INTO filament_snapshots (day, spool_id, spool_filament_id, remaining_g, label_g)"
                          " VALUES ('2026-11-01', 7, 2, 500, 1000)"))
        conn.execute(text("INSERT INTO reorder_marks (item_type, item_id, qty) VALUES ('filament', 2, 1)"))
        conn.execute(text("INSERT INTO reorder_marks (item_type, item_id, qty) VALUES ('part', 2, 1)"))  # a part: stays
    upgrade(old_db, revision="0006")
    assert scalar(old_db, "select version_num from alembic_version") == "0006"
    assert rows(old_db, "select id, sort_key, brand, label, color_name, color_hex from filament_sorts order by id") == [
        (1, "petg||#000000", None, "PETG Black", "Black", "#000000"),
        (3, "petg||#ff0000", None, "PETG Red", "Red", "#FF0000"),
    ]
    assert rows(old_db, "select slot, spool_filament_id from article_filaments order by slot") == [(1, 1), (2, 3)]
    assert rows(old_db, "select spool_id, spool_filament_id from filament_snapshots") == [(7, 1)]
    assert rows(old_db, "select item_type, item_id from reorder_marks order by id") == [("filament", 1), ("part", 2)]


def test_settings_of_the_lowest_id_stay_the_others_are_dropped_and_journalled(old_db):
    with old_db.begin() as conn:
        add_sort(conn, 1, "Brand One", "PLA", "#000000")
        add_sort(conn, 2, "Brand Two", "PLA", "#101010")
        add_sort(conn, 3, "Brand Three", "PLA", "#080808")
        add_settings(conn, 1, lead_time_days=5)
        add_settings(conn, 2, lead_time_days=9, supplier="Other Supplier")
    upgrade(old_db, revision="0006")
    assert rows(old_db, "select spool_filament_id, lead_time_days from filament_settings") == [(1, 5)]
    session = make_sessionmaker(old_db)()
    with session:
        (entry,) = session.query(JournalEntry).all()
        assert (entry.actor, entry.kind) == ("auto", "filament_sorts_merged")
        assert entry.details["into"] == 1 and entry.details["from"] == [2, 3]
        (dropped,) = entry.details["dropped_settings"]
        assert dropped["sort"] == 2 and dropped["lead_time_days"] == 9 and dropped["supplier"] == "Other Supplier"
        assert "2, 3" in entry.note and "into 1" in entry.note and "dropped" in entry.note


def test_the_next_merged_sort_brings_its_settings_when_the_lowest_has_none(old_db):
    with old_db.begin() as conn:
        add_sort(conn, 4, "Brand One", "PLA", "#000000")
        add_sort(conn, 5, "Brand Two", "PLA", "#080808")
        add_sort(conn, 6, "Brand Three", "PLA", "#101010")
        add_settings(conn, 5, lead_time_days=7)
        add_settings(conn, 6, lead_time_days=8)
    upgrade(old_db, revision="0006")
    assert rows(old_db, "select spool_filament_id, lead_time_days from filament_settings") == [(4, 7)]
    session = make_sessionmaker(old_db)()
    with session:
        (entry,) = session.query(JournalEntry).all()
        assert [d["sort"] for d in entry.details["dropped_settings"]] == [6]


def test_sorts_that_stay_apart_are_untouched_but_lose_the_brand(old_db):
    with old_db.begin() as conn:
        add_sort(conn, 1, "Brand One", "PLA", "#000000")
        add_sort(conn, 2, "Brand One", "PLA", "#333333", name="Anthracite")  # 13.4 apart
        add_sort(conn, 3, "Brand One", "PLA", "#000000", subtype="Matte", name="Matte")  # other subtype
        add_sort(conn, 4, "Brand One", "PETG", "#000000")  # other material
        add_sort(conn, 5, "Brand One", "PLA", None, name="Natural")  # no colour
        add_settings(conn, 2)
    upgrade(old_db, revision="0006")
    assert rows(old_db, "select id, sort_key, label from filament_sorts order by id") == [
        (1, "pla||#000000", "PLA Black"),
        (2, "pla||#333333", "PLA Anthracite"),
        (3, "pla|matte|#000000", "PLA Matte"),
        (4, "petg||#000000", "PETG Black"),
        (5, "pla||", "PLA Natural"),
    ]
    assert scalar(old_db, "select count(*) from journal") == 0  # nothing merged, nothing to write
    assert scalar(old_db, "select count(*) from filament_settings") == 1


def test_chain_of_colours_is_not_followed(old_db):
    with old_db.begin() as conn:
        add_sort(conn, 1, "A", "PLA", "#000000")
        add_sort(conn, 2, "B", "PLA", "#101010")  # 4 from the first
        add_sort(conn, 3, "C", "PLA", "#202020")  # 4 from the second, 8 from the first
    upgrade(old_db, revision="0006")
    assert [r[0] for r in rows(old_db, "select id from filament_sorts order by id")] == [1, 3]


def test_a_name_comes_from_a_merged_sort_when_the_lowest_has_none(old_db):
    with old_db.begin() as conn:
        add_sort(conn, 1, "A", "PLA", "#000000", name=None)
        add_sort(conn, 2, "B", "PLA", "#080808", name="Schwarz")
    upgrade(old_db, revision="0006")
    assert rows(old_db, "select label, color_name from filament_sorts") == [("PLA Schwarz", "Schwarz")]


def test_a_merged_sort_joins_the_nearest_keeper_of_all(old_db):
    """Sort 2 is within 7 of keeper 1 but nearer to keeper 3: as `sync_sorts` would place its spools."""
    with old_db.begin() as conn:
        add_sort(conn, 1, "A", "PLA", "#000000")
        add_sort(conn, 2, "B", "PLA", "#1D1D1D")
        add_sort(conn, 3, "C", "PLA", "#262626")
        conn.execute(text("INSERT INTO filament_snapshots (day, spool_id, spool_filament_id, remaining_g, label_g)"
                          " VALUES ('2026-11-01', 7, 2, 500, 1000)"))
    upgrade(old_db, revision="0006")
    assert [r[0] for r in rows(old_db, "select id from filament_sorts order by id")] == [1, 3]
    assert rows(old_db, "select spool_filament_id from filament_snapshots") == [(3,)]


def test_two_open_marks_of_merged_sorts_become_one(old_db):
    with old_db.begin() as conn:
        add_sort(conn, 1, "A", "PLA", "#000000")
        add_sort(conn, 2, "B", "PLA", "#080808")
        conn.execute(text("INSERT INTO reorder_marks (item_type, item_id, qty, marked_at)"
                          " VALUES ('filament', 1, 2, '2026-11-03 10:00:00')"))
        conn.execute(text("INSERT INTO reorder_marks (item_type, item_id, qty, marked_at)"
                          " VALUES ('filament', 2, 3, '2026-11-02 10:00:00')"))
        conn.execute(text("INSERT INTO reorder_marks (item_type, item_id, qty, marked_at, received_at)"
                          " VALUES ('filament', 2, 5, '2026-10-01 10:00:00', '2026-10-05 10:00:00')"))
    upgrade(old_db, revision="0006")
    assert rows(old_db, "select item_id, qty, marked_at, received_at from reorder_marks order by marked_at") == [
        (1, 5, "2026-10-01 10:00:00", "2026-10-05 10:00:00"),
        (1, 5, "2026-11-02 10:00:00", None),
    ]


def test_transparent_sorts_merge_and_never_with_an_opaque_one(old_db):  #
    with old_db.begin() as conn:
        add_sort(conn, 1, "Brand One", "PETG", "#000000", name=None)
        add_sort(conn, 2, "Brand Two", "PETG", "00000000", name=None)
        add_sort(conn, 3, "Brand Three", "PETG", "FFFFFF00", name=None)
        add_sort(conn, 4, "Brand One", "PETG", "#00000080", name=None)
    upgrade(old_db, revision="0006")
    assert rows(old_db, "select id, label from filament_sorts order by id") == [
        (1, "PETG #000000"), (2, "PETG transparent"), (4, "PETG #000000"),
    ]
    assert scalar(old_db, "select count(*) from journal") == 1  # only 2 and 3 fell together


def test_an_empty_database_migrates_without_a_journal_entry(old_db):
    upgrade(old_db, revision="0006")
    assert scalar(old_db, "select count(*) from journal") == 0
    assert json.dumps(rows(old_db, "select * from filament_sorts")) == "[]"
