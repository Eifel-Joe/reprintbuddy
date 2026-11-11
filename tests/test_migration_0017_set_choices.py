"""Revision 0017: sets get base parts and up to two choice fields with suffixes (Spec 5.4, p340).

A position with one article becomes a base part (slot 0); the first position with a choice becomes
choice field 1, the second choice field 2 (slots 1 and 2), each option without a suffix yet. Further
choice positions, and options whose article is a base part (an article is only once in a set), are
dropped and named in the activity log. The same fixed article twice becomes one row with the sum.
Order lines stay as they are: they already hold the chosen articles, and get no variant number.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops import fullbackup as fb

from test_migration_0015_part_suppliers import downgrade

BEFORE = "0016"
REVISION = "0017"

ARTICLES = {
    1: "Frame", 2: "Plate small", 3: "Plate large", 4: "Red", 5: "Blue",
    6: "Screw", 7: "Handle", 8: "Strip", 9: "Lid",
}

# (set id, name, sku, [(qty, [article ids])]) as v0.3.17 has them; several ids are a choice.
SETS = [
    (1, "Fixed", "EX-S01", [(1, [1]), (2, [6])]),
    (2, "Two choices", "EX-S02", [(1, [1]), (1, [3, 2]), (2, [4, 5])]),
    (3, "Twice", None, [(1, [1]), (2, [1]), (1, [6])]),
    (4, "Three choices", "EX-S04", [(1, [1]), (1, [2, 3]), (1, [4, 5]), (1, [7, 8])]),
    (5, "Base and choice", "EX-S05", [(1, [2]), (1, [3]), (1, [2, 3]), (1, [4, 5])]),
    (6, "Without options", None, [(1, [1]), (1, []), (1, [6])]),  # a position whose article rows are gone
]


def fill_old(conn) -> None:
    for article_id, name in ARTICLES.items():
        conn.execute(text(
            "INSERT INTO articles (id, name, library_file_id, plate_id, file_name)"
            " VALUES (:id, :name, :id, 1, :file)"
        ), {"id": article_id, "name": name, "file": f"a{article_id}.gcode.3mf"})
    position_id = 0
    for set_id, name, sku, positions in SETS:
        conn.execute(text("INSERT INTO product_sets (id, name, sku) VALUES (:id, :name, :sku)"),
                     {"id": set_id, "name": name, "sku": sku})
        for sort, (qty, article_ids) in enumerate(positions):
            position_id += 1
            conn.execute(text("INSERT INTO set_positions (id, set_id, sort, qty) VALUES (:id, :s, :sort, :qty)"),
                         {"id": position_id, "s": set_id, "sort": sort, "qty": qty})
            for article_id in article_ids:
                conn.execute(text("INSERT INTO set_position_options (position_id, article_id) VALUES (:p, :a)"),
                             {"p": position_id, "a": article_id})
    # An open order of set 2 with "Plate large" and "Red" chosen.
    conn.execute(text("INSERT INTO orders (id, reference, order_date, status) VALUES (1, 'R-1', '2026-11-01', 'open')"))
    conn.execute(text(
        "INSERT INTO order_lines (order_id, group_no, article_id, qty, set_id, set_qty) VALUES"
        " (1, 1, 1, 1, 2, 1), (1, 1, 3, 1, 2, 1), (1, 1, 4, 2, 2, 1)"
    ))


@pytest.fixture
def old_db(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision=BEFORE)
    with engine.begin() as conn:
        fill_old(conn)
    return engine


def shape(engine, set_id: int) -> list[tuple]:
    """(slot, qty, [(article name, suffix)] in option order) per position, in order."""
    with engine.connect() as conn:
        positions = conn.execute(text(
            "SELECT id, slot, qty FROM set_positions WHERE set_id = :s ORDER BY slot, sort, id"
        ), {"s": set_id}).all()
        result = []
        for position_id, slot, qty in positions:
            options = conn.execute(text(
                "SELECT a.name, o.suffix FROM set_position_options o JOIN articles a ON a.id = o.article_id"
                " WHERE o.position_id = :p ORDER BY o.sort, a.name"
            ), {"p": position_id}).all()
            result.append((slot, qty, [tuple(o) for o in options]))
        return result


def migrated(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT area, kind, actor, details FROM activity WHERE kind = 'set_migrated' ORDER BY id"
        )).all()
    return [{"area": a, "kind": k, "actor": actor, **json.loads(d)} for a, k, actor, d in rows]


def test_fixed_positions_become_base_parts(old_db):
    upgrade(old_db, revision=REVISION)
    assert shape(old_db, 1) == [(0, 1, [("Frame", None)]), (0, 2, [("Screw", None)])]


def test_first_and_second_choice_become_choice_fields_without_suffix(old_db):
    upgrade(old_db, revision=REVISION)
    assert shape(old_db, 2) == [
        (0, 1, [("Frame", None)]),
        (1, 1, [("Plate large", None), ("Plate small", None)]),  # options sorted by name
        (2, 2, [("Blue", None), ("Red", None)]),
    ]


def test_same_fixed_article_twice_becomes_one_row_with_the_sum(old_db):
    upgrade(old_db, revision=REVISION)
    assert shape(old_db, 3) == [(0, 3, [("Frame", None)]), (0, 1, [("Screw", None)])]


def test_third_choice_is_dropped_and_logged(old_db):
    upgrade(old_db, revision=REVISION)
    assert [slot for slot, _q, _o in shape(old_db, 4)] == [0, 1, 2]
    assert shape(old_db, 4)[2] == (2, 1, [("Blue", None), ("Red", None)])
    logged = [row for row in migrated(old_db) if row["id"] == 4]
    assert logged == [{"area": "master", "kind": "set_migrated", "actor": "system", "id": 4,
                       "name": "Three choices", "dropped": ["Handle", "Strip"]}]


def test_choice_option_that_is_a_base_part_is_dropped_and_logged(old_db):
    """Both options of the first choice are base parts: the choice goes, the second moves up."""
    upgrade(old_db, revision=REVISION)
    assert shape(old_db, 5) == [
        (0, 1, [("Plate small", None)]),
        (0, 1, [("Plate large", None)]),
        (1, 1, [("Blue", None), ("Red", None)]),
    ]
    logged = [row for row in migrated(old_db) if row["id"] == 5]
    assert logged[0]["dropped"] == ["Plate large", "Plate small"]


def test_a_position_without_articles_goes(old_db):  # review of p340 (M2)
    upgrade(old_db, revision=REVISION)
    assert shape(old_db, 6) == [(0, 1, [("Frame", None)]), (0, 1, [("Screw", None)])]
    with old_db.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM set_positions p WHERE NOT EXISTS"
            " (SELECT 1 FROM set_position_options o WHERE o.position_id = p.id)"
        )).scalar() == 0


def test_sets_without_anything_dropped_are_not_logged(old_db):
    upgrade(old_db, revision=REVISION)
    assert sorted(row["id"] for row in migrated(old_db)) == [4, 5]


def test_orders_stay_as_they_were(old_db):
    with old_db.connect() as conn:
        before = conn.execute(text("SELECT id, order_id, group_no, article_id, qty, set_id, set_qty FROM order_lines")).all()
    upgrade(old_db, revision=REVISION)
    with old_db.connect() as conn:
        after = conn.execute(text(
            "SELECT id, order_id, group_no, article_id, qty, set_id, set_qty, variant_sku FROM order_lines"
        )).all()
    assert [tuple(r[:7]) for r in after] == [tuple(r) for r in before]
    assert {r[7] for r in after} == {None}


def test_slot_values_are_checked(old_db):
    upgrade(old_db, revision=REVISION)
    with pytest.raises(Exception):  # noqa: B017 - SQLite raises IntegrityError through SQLAlchemy
        with old_db.begin() as conn:
            conn.execute(text("UPDATE set_positions SET slot = 3 WHERE set_id = 1"))
    with pytest.raises(Exception):  # noqa: B017 - one choice field per slot and set
        with old_db.begin() as conn:
            conn.execute(text("UPDATE set_positions SET slot = 1 WHERE set_id = 2 AND slot = 2"))


def test_downgrade_gives_choice_positions_back(old_db):
    upgrade(old_db, revision=REVISION)
    downgrade(old_db, BEFORE)
    with old_db.connect() as conn:
        columns = {r[1] for r in conn.execute(text("PRAGMA table_info(set_positions)"))}
        options = {r[1] for r in conn.execute(text("PRAGMA table_info(set_position_options)"))}
        lines = {r[1] for r in conn.execute(text("PRAGMA table_info(order_lines)"))}
        choice = conn.execute(text(
            "SELECT p.sort, group_concat(o.article_id) FROM set_positions p"
            " JOIN set_position_options o ON o.position_id = p.id WHERE p.set_id = 2"
            " GROUP BY p.id ORDER BY p.sort"
        )).all()
    assert "slot" not in columns and {"suffix", "sort"}.isdisjoint(options) and "variant_sku" not in lines
    assert [sorted(map(int, ids.split(","))) for _sort, ids in choice] == [[1], [2, 3], [4, 5]]


def test_an_upgraded_database_has_the_structure_of_a_new_one(old_db, tmp_path):
    upgrade(old_db)
    old_db.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(tmp_path / "old.db") == fb._structure(tmp_path / "new.db")


def test_a_full_backup_of_0_3_17_restores_and_then_looks_like_after_the_update(old_db, tmp_path):
    """A backup taken with 0.3.17 (schema 0016) is accepted; the start migrates it like an update
    (the restore keeps a database at its own revision, tests/ops/test_fullbackup.py)."""
    old_db.dispose()
    work = tmp_path / "work"
    work.mkdir()
    created = fb.create_backup(tmp_path / "old.db", work_dir=work, password=None,
                               now=datetime(2026, 11, 11, 12, 0, tzinfo=UTC))
    manifest = fb.read_manifest(created.path)
    assert manifest.schema_revision == BEFORE
    restored = work / "candidate.db"
    fb.extract_database(created.path, manifest, None, restored)
    fb.verify_database(restored, manifest, work_dir=work)
    engine = make_engine(restored)
    upgrade(engine)  # what the start does with the restored database
    assert shape(engine, 2) == [
        (0, 1, [("Frame", None)]),
        (1, 1, [("Plate large", None), ("Plate small", None)]),
        (2, 2, [("Blue", None), ("Red", None)]),
    ]
    with engine.connect() as conn:
        lines = conn.execute(text("SELECT article_id, qty, variant_sku FROM order_lines WHERE order_id = 1 ORDER BY id")).all()
    assert [tuple(r) for r in lines] == [(1, 1, None), (3, 1, None), (4, 2, None)]
