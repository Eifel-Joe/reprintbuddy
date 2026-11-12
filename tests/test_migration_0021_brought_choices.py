"""Revision 0021: inner sets bring their choice parts (Spec 5.4, 0.4.4 p441).

The fixed variant of an inner set (`set_position_sets.choice1_id/choice2_id`, 0.4.3) goes: the inner
set brings its choice fields into the outer set and the choice is made when ordering. The activity
log names every set whose variants arise anew, with the inner sets whose fixed variant ended, and an
article that now stands twice (every option counts since 0.4.4; that set then shows "broken").
Orders stay as they are (Spec 5.4, "Update auf den Stand mit mitgebrachten Wahlteilen"); a backup of
0.4.3 restores and migrates.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.domain import catalog, orders
from reprintbuddy.ops import fullbackup as fb

from test_migration_0015_part_suppliers import downgrade

BEFORE = "0020"
REVISION = "0021"

ARTICLES = ("Frame", "Manual", "Plate A", "Plate B", "Screws", "Cover", "Hook")


def fill_old(conn) -> None:
    """Kit (EX-K): Frame + Manual + choice Plate A -A / Plate B -B. Bundle (EX-BU) = Kit fixed on
    Plate A + Screws. Wrapped (no number) = Bundle + Cover: its variants change through Bundle.
    Plain (EX-P) = Screws + Hook, and Box (EX-BX) = Plain + Cover: no fixed variant anywhere.
    Clash (EX-C) = Kit fixed on Plate A + Plate B: allowed in 0.4.3, twice in 0.4.4."""
    for i, name in enumerate(ARTICLES, start=1):
        conn.execute(text(
            "INSERT INTO articles (id, name, library_file_id, plate_id, file_name, stock, reserved)"
            f" VALUES ({i}, '{name}', 1, {i}, 'f.3mf', 5, 0)"
        ))
    for set_id, name, sku in ((1, "Kit", "'EX-K'"), (2, "Bundle", "'EX-BU'"), (3, "Wrapped", "NULL"),
                              (4, "Plain", "'EX-P'"), (5, "Box", "'EX-BX'"), (6, "Clash", "'EX-C'")):
        conn.execute(text(f"INSERT INTO product_sets (id, name, sku) VALUES ({set_id}, '{name}', {sku})"))
    position = 0

    def row(set_id, qty, *, article=None, inner=None, choice=None, slot=0, options=()):
        nonlocal position
        position += 1
        conn.execute(text(
            f"INSERT INTO set_positions (id, set_id, sort, qty, slot) VALUES ({position}, {set_id}, {position}, {qty}, {slot})"
        ))
        if article is not None:
            conn.execute(text(
                f"INSERT INTO set_position_options (position_id, article_id, suffix, sort) VALUES ({position}, {article}, NULL, 0)"
            ))
        if inner is not None:
            conn.execute(text(
                "INSERT INTO set_position_sets (position_id, set_id, choice1_id, choice2_id)"
                f" VALUES ({position}, {inner}, {choice if choice is not None else 'NULL'}, NULL)"
            ))
        for sort, (article_id, suffix) in enumerate(options):
            conn.execute(text(
                "INSERT INTO set_position_options (position_id, article_id, suffix, sort)"
                f" VALUES ({position}, {article_id}, '{suffix}', {sort})"
            ))

    row(1, 1, article=1)
    row(1, 1, article=2)
    row(1, 1, slot=1, options=((3, "-A"), (4, "-B")))
    row(2, 1, inner=1, choice=3)
    row(2, 2, article=5)
    row(3, 1, inner=2)
    row(3, 1, article=6)
    row(4, 1, article=5)
    row(4, 1, article=7)
    row(5, 1, inner=4)
    row(5, 1, article=6)
    row(6, 1, inner=1, choice=3)
    row(6, 1, article=4)
    conn.execute(text("INSERT INTO orders (id, reference, order_date, status) VALUES (1, 'R-1', '2026-11-12', 'open')"))
    path = json.dumps([[1, "EX-K-A", 1]])
    conn.execute(text(
        "INSERT INTO order_lines (id, order_id, group_no, article_id, qty, set_id, set_qty, variant_sku, inner_path)"
        f" VALUES (1, 1, 1, 1, 1, 2, 1, NULL, '{path}'), (2, 1, 1, 3, 1, 2, 1, NULL, '{path}'),"
        " (3, 1, 1, 5, 2, 2, 1, NULL, NULL)"
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


def _log(engine) -> dict[str, dict]:
    found = {}
    for kind, actor, area, details in _rows(engine, "SELECT kind, actor, area, details FROM activity ORDER BY id"):
        assert (kind, actor, area) == ("set_choices_brought", "system", "master")
        entry = json.loads(details)
        found[entry["name"]] = entry
    return found


LINES = "SELECT id, order_id, group_no, article_id, qty, set_id, set_qty, variant_sku, inner_path FROM order_lines ORDER BY id"
INNER = "SELECT position_id, set_id FROM set_position_sets ORDER BY position_id"


def test_fixed_variant_becomes_brought_and_is_logged(old_db):
    inner = _rows(old_db, INNER)
    upgrade(old_db, revision=REVISION)
    with old_db.connect() as conn:
        columns = [r[1] for r in conn.execute(text("PRAGMA table_info(set_position_sets)"))]
    assert columns == ["position_id", "set_id"]
    assert _rows(old_db, INNER) == inner
    log = _log(old_db)
    assert log["Bundle"] == {"id": 2, "name": "Bundle", "inner": ["Kit"], "twice": []}
    assert log["Wrapped"] == {"id": 3, "name": "Wrapped", "inner": ["Kit"], "twice": []}  # through Bundle
    assert log["Clash"] == {"id": 6, "name": "Clash", "inner": ["Kit"], "twice": ["Plate B"]}


def test_sets_without_fixed_variant_are_not_logged(old_db):
    upgrade(old_db, revision=REVISION)
    assert set(_log(old_db)) == {"Bundle", "Wrapped", "Clash"}


def test_orders_stay_as_they_were(old_db):
    before = _rows(old_db, LINES)
    upgrade(old_db, revision=REVISION)
    assert _rows(old_db, LINES) == before


def test_sets_read_with_brought_choices_after_the_update(old_db):
    upgrade(old_db)  # the code of today reads the database of today (later revisions add columns)
    with make_sessionmaker(old_db)() as s:
        rows = {row.name: row for row in catalog.list_sets(s)}
        assert [v.sku for v in rows["Bundle"].variants] == ["EX-BU-A", "EX-BU-B"]
        assert rows["Bundle"].orderable
        assert rows["Wrapped"].incomplete == "number_missing" and not rows["Wrapped"].orderable
        assert rows["Clash"].incomplete == "broken" and not rows["Clash"].orderable
        assert rows["Box"].incomplete is None and rows["Box"].orderable
        view = orders.list_orders(s, "open")[0]
        assert view.groups[0].inner[0].variant_sku == "EX-K-A"  # as ordered


def test_downgrade_brings_first_options_back(old_db):
    upgrade(old_db, revision=REVISION)
    downgrade(old_db, BEFORE)
    assert _rows(old_db, "SELECT position_id, set_id, choice1_id, choice2_id FROM set_position_sets ORDER BY position_id") == [
        (4, 1, 3, None), (6, 2, None, None), (10, 4, None, None), (12, 1, 3, None),
    ]


def _backup_and_restore(tmp_path, source, revision):
    work = tmp_path / "work"
    work.mkdir()
    created = fb.create_backup(source, work_dir=work, password=None, now=datetime(2026, 11, 12, 12, 0, tzinfo=UTC))
    manifest = fb.read_manifest(created.path)
    assert manifest.schema_revision == revision
    restored = work / "candidate.db"
    fb.extract_database(created.path, manifest, None, restored)
    fb.verify_database(restored, manifest, work_dir=work)
    return restored


def test_full_backup_of_0_4_3_restores_with_0_4_4(tmp_path, old_db):
    before = _rows(old_db, LINES)
    old_db.dispose()
    restored = _backup_and_restore(tmp_path, tmp_path / "old.db", BEFORE)
    engine = make_engine(restored)
    upgrade(engine)
    assert _rows(engine, LINES) == before
    assert set(_log(engine)) == {"Bundle", "Wrapped", "Clash"}
    engine.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(restored) == fb._structure(tmp_path / "new.db")
