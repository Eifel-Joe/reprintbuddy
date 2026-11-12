"""Revision 0020: sets in sets (Spec 5.4, 0.4.3 p423).

A base row of a set may now point at another set: `set_position_sets` holds the inner set and its
fixed variant (the chosen article per choice field). `order_lines.inner_path` keeps the way through
the inner sets as it was when the order was placed. Sets and orders of earlier versions stay as they
are (Spec 5.4, "Update auf den Stand mit Sets in Sets"); a backup of 0.4.2 restores and migrates.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.domain import catalog, orders
from reprintbuddy.ops import fullbackup as fb

from test_migration_0015_part_suppliers import downgrade

BEFORE = "0019"
REVISION = "0020"


def fill_old(conn) -> None:
    for i, name in enumerate(("Frame", "Plate A", "Plate B", "Screw"), start=1):
        conn.execute(text(
            "INSERT INTO articles (id, name, library_file_id, plate_id, file_name, stock, reserved)"
            f" VALUES ({i}, '{name}', 1, {i}, 'f.3mf', 5, 1)"
        ))
    conn.execute(text("INSERT INTO product_sets (id, name, sku) VALUES (1, 'Kit', 'EX-S1')"))
    conn.execute(text("INSERT INTO set_positions (id, set_id, sort, qty, slot) VALUES (1, 1, 0, 2, 0)"))
    conn.execute(text("INSERT INTO set_position_options (position_id, article_id, suffix, sort) VALUES (1, 1, NULL, 0)"))
    conn.execute(text("INSERT INTO set_positions (id, set_id, sort, qty, slot) VALUES (2, 1, 0, 1, 1)"))
    conn.execute(text("INSERT INTO set_position_options (position_id, article_id, suffix, sort) VALUES (2, 2, '-A', 0)"))
    conn.execute(text("INSERT INTO set_position_options (position_id, article_id, suffix, sort) VALUES (2, 3, '-B', 1)"))
    conn.execute(text(
        "INSERT INTO orders (id, reference, order_date, status) VALUES (1, 'R-1', '2026-11-10', 'open')"
    ))
    conn.execute(text(
        "INSERT INTO order_lines (id, order_id, group_no, article_id, qty, set_id, set_qty, variant_sku)"
        " VALUES (1, 1, 1, 1, 2, 1, 1, 'EX-S1-A'), (2, 1, 1, 2, 1, 1, 1, 'EX-S1-A')"
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


SETS = "SELECT p.set_id, p.slot, p.qty, o.article_id, o.suffix FROM set_positions p JOIN set_position_options o ON o.position_id = p.id ORDER BY p.id, o.sort"
LINES = "SELECT id, order_id, group_no, article_id, qty, set_id, set_qty, variant_sku FROM order_lines ORDER BY id"


def test_sets_and_orders_keep_their_rows(old_db):
    before = (_rows(old_db, SETS), _rows(old_db, LINES))
    upgrade(old_db, revision=REVISION)
    assert (_rows(old_db, SETS), _rows(old_db, LINES)) == before
    assert _rows(old_db, "SELECT inner_path FROM order_lines") == [(None,), (None,)]
    assert _rows(old_db, "SELECT count(*) FROM set_position_sets") == [(0,)]


def test_existing_sets_and_orders_read_as_before(old_db):
    upgrade(old_db, revision=REVISION)
    sm = make_sessionmaker(old_db)
    with sm() as s:
        row = catalog.list_sets(s)[0]
        assert [(b.qty, b.name, b.set_id) for b in row.base] == [(2, "Frame", None)]
        assert [v.sku for v in row.variants] == ["EX-S1-A", "EX-S1-B"]
        view = orders.list_orders(s, "open")[0]
        assert view.groups[0].lines == [("Frame", 2), ("Plate A", 1)] and view.groups[0].inner == []


def test_inner_set_row_points_at_set_and_articles(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        conn.execute(text("INSERT INTO product_sets (id, name) VALUES (2, 'Bundle')"))
        conn.execute(text("INSERT INTO set_positions (id, set_id, sort, qty, slot) VALUES (3, 2, 0, 1, 0)"))
        conn.execute(text(
            "INSERT INTO set_position_sets (position_id, set_id, choice1_id, choice2_id) VALUES (3, 1, 2, NULL)"
        ))
    assert _rows(old_db, "SELECT position_id, set_id, choice1_id, choice2_id FROM set_position_sets") == [(3, 1, 2, None)]
    with pytest.raises(IntegrityError):
        with old_db.begin() as conn:
            conn.execute(text("INSERT INTO set_positions (id, set_id, sort, qty, slot) VALUES (4, 2, 1, 1, 0)"))
            conn.execute(text("INSERT INTO set_position_sets (position_id, set_id) VALUES (4, 99)"))


def test_downgrade_drops_inner_set_rows_and_keeps_the_rest(old_db):
    before = _rows(old_db, SETS)
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        conn.execute(text("INSERT INTO product_sets (id, name) VALUES (2, 'Bundle')"))
        conn.execute(text("INSERT INTO set_positions (id, set_id, sort, qty, slot) VALUES (3, 2, 0, 1, 0)"))
        conn.execute(text("INSERT INTO set_position_sets (position_id, set_id, choice1_id) VALUES (3, 1, 2)"))
        conn.execute(text("UPDATE order_lines SET inner_path = '[[1, null, 1]]' WHERE id = 1"))
    downgrade(old_db, BEFORE)
    assert _rows(old_db, SETS) == before
    assert _rows(old_db, "SELECT id FROM set_positions ORDER BY id") == [(1,), (2,)]
    with old_db.connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
        columns = {r[1] for r in conn.execute(text("PRAGMA table_info(order_lines)"))}
    assert "set_position_sets" not in tables and "inner_path" not in columns


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


def test_full_backup_of_0_4_2_restores_with_0_4_3(tmp_path, old_db):
    before = (_rows(old_db, SETS), _rows(old_db, LINES))
    old_db.dispose()
    restored = _backup_and_restore(tmp_path, tmp_path / "old.db", BEFORE)
    engine = make_engine(restored)
    upgrade(engine)
    assert (_rows(engine, SETS), _rows(engine, LINES)) == before
    engine.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(restored) == fb._structure(tmp_path / "new.db")
