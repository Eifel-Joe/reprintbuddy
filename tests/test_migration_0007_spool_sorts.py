"""Revision 0007: spool sorts with the brand, a group of spool sorts per slot (Spec 4, 5.2, 9.1).

The migration cannot know the brands (the sorts of 0006 have none, the spools only come over the
network). It marks every old sort as legacy and drops all slot mappings; the settings, open marks
and daily rows of the old sorts are moved at run time by `filament.convert_legacy`.
"""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from reprintbuddy import db as dbmod
from reprintbuddy.db import make_engine, upgrade


@pytest.fixture
def old_db(tmp_path):
    """A database at revision 0006 (sorts without brand, one sort per slot)."""
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision="0006")
    return engine


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def fill(conn):
    conn.execute(text(
        "INSERT INTO filament_sorts (id, sort_key, material, color_hex, color_name, label)"
        " VALUES (1, 'petg||#000000', 'PETG', '#000000', 'Black', 'PETG Black'),"
        " (2, 'pla||#ff0000', 'PLA', '#FF0000', NULL, 'PLA #FF0000')"
    ))
    conn.execute(text("INSERT INTO articles (id, name, library_file_id, plate_id, file_name)"
                      " VALUES (1, 'Template A', 1, 1, 'a.gcode.3mf')"))
    conn.execute(text("INSERT INTO article_filaments (article_id, slot, grams, material, color_hex, spool_filament_id)"
                      " VALUES (1, 1, 10, 'PETG', '#000000', 1), (1, 2, 5, 'PLA', '#FF0000', NULL)"))
    conn.execute(text("INSERT INTO filament_settings (spool_filament_id, lead_time_days) VALUES (1, 4)"))
    conn.execute(text("INSERT INTO filament_snapshots (day, spool_id, spool_filament_id, remaining_g, label_g)"
                      " VALUES ('2026-11-01', 7, 1, 500, 1000)"))
    conn.execute(text("INSERT INTO reorder_marks (item_type, item_id, qty) VALUES ('filament', 1, 2)"))


def test_old_sorts_become_legacy_and_keep_what_points_at_them(old_db):
    with old_db.begin() as conn:
        fill(conn)
    upgrade(old_db)
    assert scalar(old_db, "select version_num from alembic_version") == "0007"
    assert rows(old_db, "select id, sort_key, legacy, label from filament_sorts order by id") == [
        (1, "legacy:1", 1, "PETG Black"),
        (2, "legacy:2", 1, "PLA #FF0000"),
    ]
    # settings, marks and daily rows stay on the legacy sort until the run-time conversion
    assert rows(old_db, "select spool_filament_id, lead_time_days from filament_settings") == [(1, 4)]
    assert rows(old_db, "select spool_filament_id from filament_snapshots") == [(1,)]
    assert rows(old_db, "select item_type, item_id, qty from reorder_marks") == [("filament", 1, 2)]
    assert scalar(old_db, "select count(*) from journal") == 0


def test_slot_mappings_are_dropped_the_slots_stay(old_db):
    with old_db.begin() as conn:
        fill(conn)
    upgrade(old_db)
    columns = {c["name"] for c in inspect(old_db).get_columns("article_filaments")}
    assert "spool_filament_id" not in columns
    assert rows(old_db, "select article_id, slot, grams, material, color_hex from article_filaments order by slot") == [
        (1, 1, 10.0, "PETG", "#000000"), (1, 2, 5.0, "PLA", "#FF0000"),
    ]
    assert scalar(old_db, "select count(*) from article_filament_sorts") == 0


def test_a_new_sort_defaults_to_not_legacy(old_db):
    upgrade(old_db)
    with old_db.begin() as conn:
        conn.execute(text("INSERT INTO filament_sorts (sort_key, brand, material, label)"
                          " VALUES ('brand|pla||#000000', 'Brand', 'PLA', 'Brand PLA')"))
    assert rows(old_db, "select legacy from filament_sorts") == [(0,)]


def test_one_reordered_sort_per_slot(old_db):
    upgrade(old_db)
    with old_db.begin() as conn:
        conn.execute(text("INSERT INTO articles (id, name, library_file_id, plate_id, file_name)"
                          " VALUES (1, 'Template A', 1, 1, 'a.gcode.3mf')"))
        conn.execute(text("INSERT INTO filament_sorts (id, sort_key, material, label)"
                          " VALUES (1, 'a', 'PLA', 'A'), (2, 'b', 'PLA', 'B')"))
        conn.execute(text("INSERT INTO article_filament_sorts (article_id, slot, sort_id, reordered)"
                          " VALUES (1, 1, 1, 1), (1, 1, 2, 0), (1, 2, 2, 1)"))
    with pytest.raises(IntegrityError):
        with old_db.begin() as conn:
            conn.execute(text("UPDATE article_filament_sorts SET reordered = 1 WHERE slot = 1 AND sort_id = 2"))
    with pytest.raises(IntegrityError):  # one sort is ticked once per slot
        with old_db.begin() as conn:
            conn.execute(text("INSERT INTO article_filament_sorts (article_id, slot, sort_id) VALUES (1, 1, 1)"))


def test_the_group_refers_to_existing_sorts_and_articles(old_db):
    upgrade(old_db)
    with pytest.raises(IntegrityError):
        with old_db.begin() as conn:
            conn.execute(text("INSERT INTO article_filament_sorts (article_id, slot, sort_id) VALUES (9, 1, 9)"))


def test_scripts_dir_has_0007_on_top_of_0006():
    versions = Path(dbmod.__file__).parent / "migrations" / "versions"
    text_0007 = next(versions.glob("0007*")).read_text(encoding="utf-8")
    assert 'down_revision = "0006"' in text_0007


def test_daily_rows_have_an_index_by_spool(old_db):
    """The daily rows follow their spool on every spool list (`filament._follow_spools`)."""
    upgrade(old_db)
    indexes = {i["name"]: i["column_names"] for i in inspect(old_db).get_indexes("filament_snapshots")}
    assert indexes.get("ix_filament_snapshots_spool") == ["spool_id"]
