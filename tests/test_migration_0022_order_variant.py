"""Revision 0022: an order line of an article made of parts built on order keeps its variant (p360, Spec 5.6).

Until 0.4.4 a line kept only the variant number as ordered; after its suffix changed, the variant could
be dropped although an open order held it. The new column `order_lines.variant_id` names the variant row
itself (the article without choices: the article). The update fills it where the number still matches
a variant of that article; a line whose variant was renamed since keeps the check by number. A backup of
0.4.4 restores and migrates.
"""

from datetime import UTC, datetime

from sqlalchemy import text

import pytest
from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops import fullbackup as fb

from test_migration_0015_part_suppliers import downgrade

BEFORE = "0021"
REVISION = "0022"

LINES = "SELECT id, order_id, group_no, article_id, qty, set_id, set_qty, variant_sku, assembled_id, inner_path FROM order_lines ORDER BY id"


def fill_old(conn) -> None:
    """Parts 1, 2; Hose (3, EX-H) built on order with variants EX-H-A (4) and EX-H-B (5); Strap (6, EX-W)
    built on order without choices. Order 1: Hose EX-H-A, Hose EX-H-OLD (renamed since), Strap, part 1
    on its own."""
    for i in (1, 2):
        conn.execute(text(
            "INSERT INTO articles (id, name, library_file_id, plate_id, file_name, stock, reserved)"
            f" VALUES ({i}, 'Part {i}', 1, {i}, 'f.3mf', 5, 0)"
        ))
    conn.execute(text("INSERT INTO articles (id, name, sku, kind, assembly_mode) VALUES (3, 'Hose', 'EX-H', 'assembled', 'order')"))
    conn.execute(text("INSERT INTO articles (id, name, sku, kind, parent_id, variant_key) VALUES (4, 'Hose', 'EX-H-A', 'variant', 3, '1')"))
    conn.execute(text("INSERT INTO articles (id, name, sku, kind, parent_id, variant_key) VALUES (5, 'Hose', 'EX-H-B', 'variant', 3, '2')"))
    conn.execute(text("INSERT INTO articles (id, name, sku, kind, assembly_mode) VALUES (6, 'Strap', 'EX-W', 'assembled', 'order')"))
    conn.execute(text("INSERT INTO orders (id, reference, order_date, status) VALUES (1, 'R-1', '2026-11-12', 'open')"))
    conn.execute(text(
        "INSERT INTO order_lines (id, order_id, group_no, article_id, qty, set_id, set_qty, variant_sku, assembled_id, inner_path)"
        " VALUES (1, 1, 1, 1, 1, NULL, 1, 'EX-H-A', 3, NULL), (2, 1, 1, 2, 1, NULL, 1, 'EX-H-A', 3, NULL),"
        " (3, 1, 2, 2, 1, NULL, 1, 'EX-H-OLD', 3, NULL),"
        " (4, 1, 3, 1, 2, NULL, 2, 'EX-W', 6, NULL),"
        " (5, 1, 4, 1, 1, NULL, NULL, NULL, NULL, NULL)"
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


def test_lines_get_their_variant_where_the_number_still_matches(old_db):
    before = _rows(old_db, LINES)
    upgrade(old_db, revision=REVISION)
    assert _rows(old_db, LINES) == before  # nothing else changes
    assert _rows(old_db, "SELECT id, variant_id FROM order_lines ORDER BY id") == [
        (1, 4), (2, 4),  # EX-H-A
        (3, None),  # renamed since: the check by number stays
        (4, 6),  # without choices: the article itself
        (5, None),  # a print article ordered on its own
    ]


def test_downgrade_drops_the_column(old_db):
    before = _rows(old_db, LINES)
    upgrade(old_db, revision=REVISION)
    downgrade(old_db, BEFORE)
    with old_db.connect() as conn:
        columns = [r[1] for r in conn.execute(text("PRAGMA table_info(order_lines)"))]
    assert "variant_id" not in columns
    assert _rows(old_db, LINES) == before


def test_full_backup_of_0_4_4_restores_with_the_variant(tmp_path, old_db):
    old_db.dispose()
    work = tmp_path / "work"
    work.mkdir()
    created = fb.create_backup(tmp_path / "old.db", work_dir=work, password=None, now=datetime(2026, 11, 12, 12, 0, tzinfo=UTC))
    manifest = fb.read_manifest(created.path)
    assert manifest.schema_revision == BEFORE
    restored = work / "candidate.db"
    fb.extract_database(created.path, manifest, None, restored)
    fb.verify_database(restored, manifest, work_dir=work)
    engine = make_engine(restored)
    upgrade(engine)
    assert _rows(engine, "SELECT id, variant_id FROM order_lines ORDER BY id") == [(1, 4), (2, 4), (3, None), (4, 6), (5, None)]
    engine.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(restored) == fb._structure(tmp_path / "new.db")
