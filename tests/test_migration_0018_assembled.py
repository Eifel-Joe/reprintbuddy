"""Revision 0018: articles made of parts and their variants (Spec 5.6, 0.4.0 p341).

Every article of v0.4.0 before this update is a print article (`kind` 'print') sold alone; the
columns of the plate become optional for the new kinds, so `articles` is rebuilt (written out, as
`filament_settings` in 0016). The bill of materials lives in two new tables, and an order line can
name the assembled article its group stands for. Rows of other tables that point at an article
keep pointing at it.
"""

import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops import fullbackup as fb

from test_migration_0015_part_suppliers import downgrade

BEFORE = "0017"
REVISION = "0018"

ARTICLE_COLUMNS = (
    "id, name, archived_at, sku, label_text, library_file_id, plate_id, file_name, file_fingerprint,"
    " units_per_print, min_stock, buffer, target_model, printer_id, needs_assembly, auto_enabled, paused,"
    " pause_reason, consecutive_failures, stock, reserved, provisional, awaiting_assembly, thumbnail, created_at"
)


def fill_old(conn) -> None:
    conn.execute(text(
        f"INSERT INTO articles ({ARTICLE_COLUMNS}) VALUES"
        " (1, 'Segment', NULL, 'EX-P1', 'Seg', 11, 1, 'seg.gcode.3mf', 'abc', 4, 10, 5, 'X1C', 3, 0, 1, 1,"
        "  'file', 2, 12, 2, 1, 0, x'89504e47', '2026-11-01 08:00:00'),"
        " (2, 'Clip', '2026-11-02 09:00:00', NULL, NULL, 12, 2, 'clip.3mf', NULL, 1, 0, 0, NULL, NULL, 1, 0, 0,"
        "  NULL, 0, 0, 0, 0, 3, NULL, '2026-11-01 08:00:00'),"
        " (3, 'Lid', NULL, 'EX-P3', NULL, 13, 1, 'lid.3mf', NULL, 1, 2, 0, NULL, NULL, 0, 0, 0,"
        "  NULL, 0, 5, 1, 0, 0, NULL, '2026-11-01 08:00:00')"
    ))
    conn.execute(text("INSERT INTO parts (id, name) VALUES (1, 'Screw')"))
    conn.execute(text("INSERT INTO article_parts (article_id, part_id, qty_per_unit) VALUES (1, 1, 2)"))
    conn.execute(text("INSERT INTO article_filaments (article_id, slot, grams) VALUES (1, 1, 12.5)"))
    conn.execute(text("INSERT INTO product_sets (id, name, sku) VALUES (1, 'Kit', 'EX-S1')"))
    conn.execute(text("INSERT INTO set_positions (id, set_id, sort, qty, slot) VALUES (1, 1, 0, 1, 0), (2, 1, 1, 1, 1)"))
    conn.execute(text(
        "INSERT INTO set_position_options (position_id, article_id, suffix, sort) VALUES (1, 1, NULL, 0),"
        " (2, 2, '-A', 0), (2, 3, '-B', 1)"
    ))
    conn.execute(text("INSERT INTO orders (id, reference, order_date, status) VALUES (1, 'R-1', '2026-11-03', 'open')"))
    conn.execute(text(
        "INSERT INTO order_lines (order_id, group_no, article_id, qty, set_id, set_qty, variant_sku) VALUES"
        " (1, 1, 1, 1, 1, 1, 'EX-S1-B'), (1, 1, 3, 1, 1, 1, 'EX-S1-B'), (1, 2, 1, 1, NULL, NULL, NULL)"
    ))
    conn.execute(text(
        "INSERT INTO journal (at, actor, kind, article_id, qty) VALUES ('2026-11-03 10:00:00', 'user', 'count', 1, 12)"
    ))
    conn.execute(text(
        "INSERT INTO work_queue (article_id, prints, origin, position) VALUES (1, 2, 'manual', 1)"
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


def test_existing_articles_become_print_articles_sold_alone(old_db):
    before = _rows(old_db, f"SELECT {ARTICLE_COLUMNS} FROM articles ORDER BY id")
    upgrade(old_db, revision=REVISION)
    after = _rows(old_db, f"SELECT {ARTICLE_COLUMNS} FROM articles ORDER BY id")
    assert after == before
    new = _rows(old_db, "SELECT kind, sold_alone, assembly_mode, parent_id, variant_key FROM articles ORDER BY id")
    assert new == [("print", 1, None, None, None)] * 3


def test_rows_that_point_at_articles_survive(old_db):
    queries = [
        "SELECT * FROM journal", "SELECT * FROM order_lines", "SELECT * FROM work_queue",
        "SELECT * FROM set_position_options", "SELECT * FROM article_parts", "SELECT * FROM article_filaments",
    ]
    before = [_rows(old_db, q) for q in queries]
    upgrade(old_db, revision=REVISION)
    after = [_rows(old_db, q) for q in queries]
    assert [rows for rows in after[:1] + after[2:]] == [rows for rows in before[:1] + before[2:]]
    assert [r[:-1] for r in after[1]] == before[1]  # order lines: one new column at the end
    assert {r[-1] for r in after[1]} == {None}
    with old_db.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_key_check")).all() == []


def test_new_tables_and_columns_exist(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
        lines = {r[1] for r in conn.execute(text("PRAGMA table_info(order_lines)"))}
    assert {"article_options", "article_components"} <= tables
    assert "assembled_id" in lines


def _insert(conn, sql: str) -> None:
    conn.execute(text(sql))


def test_print_article_needs_its_plate(old_db):
    upgrade(old_db, revision=REVISION)
    with pytest.raises(Exception):  # noqa: B017 - IntegrityError through SQLAlchemy
        with old_db.begin() as conn:
            _insert(conn, "INSERT INTO articles (name, kind) VALUES ('No plate', 'print')")
    with old_db.begin() as conn:  # an assembled article has none
        _insert(conn, "INSERT INTO articles (id, name, kind, assembly_mode) VALUES (10, 'Box', 'assembled', 'stock')")
    with pytest.raises(Exception):  # noqa: B017 - assembled needs its mode
        with old_db.begin() as conn:
            _insert(conn, "INSERT INTO articles (name, kind) VALUES ('Lamp', 'assembled')")
    with pytest.raises(Exception):  # noqa: B017 - only assembled has a mode
        with old_db.begin() as conn:
            _insert(conn, "UPDATE articles SET assembly_mode = 'stock' WHERE id = 1")
    with pytest.raises(Exception):  # noqa: B017 - unknown kind
        with old_db.begin() as conn:
            _insert(conn, "UPDATE articles SET kind = 'set' WHERE id = 1")


def test_variant_needs_parent_and_key(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        _insert(conn, "INSERT INTO articles (id, name, kind, assembly_mode) VALUES (10, 'Hose', 'assembled', 'order')")
        _insert(conn, "INSERT INTO articles (id, name, kind, parent_id, variant_key, sku)"
                      " VALUES (11, 'Hose', 'variant', 10, '1', 'EX-H1-L32')")
    with pytest.raises(Exception):  # noqa: B017 - no key
        with old_db.begin() as conn:
            _insert(conn, "INSERT INTO articles (name, kind, parent_id) VALUES ('Hose', 'variant', 10)")
    with pytest.raises(Exception):  # noqa: B017 - a print article is no variant
        with old_db.begin() as conn:
            _insert(conn, "UPDATE articles SET parent_id = 10, variant_key = '2' WHERE id = 1")
    with pytest.raises(Exception):  # noqa: B017 - one active row per variant
        with old_db.begin() as conn:
            _insert(conn, "INSERT INTO articles (name, kind, parent_id, variant_key) VALUES ('Hose', 'variant', 10, '1')")
    with old_db.begin() as conn:  # an archived row keeps its key; a new active one may take it
        _insert(conn, "UPDATE articles SET archived_at = '2026-11-04 00:00:00' WHERE id = 11")
        _insert(conn, "INSERT INTO articles (name, kind, parent_id, variant_key) VALUES ('Hose', 'variant', 10, '1')")


def test_components_check_their_field(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        _insert(conn, "INSERT INTO articles (id, name, kind, assembly_mode) VALUES (10, 'Hose', 'assembled', 'order')")
        _insert(conn, "INSERT INTO article_options (id, article_id, field, sort, suffix) VALUES (1, 10, 1, 0, '-L32')")
        _insert(conn, "INSERT INTO article_components (article_id, field, option_id, part_id, qty, sort)"
                      " VALUES (10, 0, NULL, 1, 12, 0), (10, 1, 1, 3, 1, 0)")
    for bad in (
        "INSERT INTO article_components (article_id, field, option_id, part_id, qty) VALUES (10, 0, 1, 1, 1)",
        "INSERT INTO article_components (article_id, field, option_id, part_id, qty) VALUES (10, 1, NULL, 1, 1)",
        "INSERT INTO article_components (article_id, field, option_id, part_id, qty) VALUES (10, 0, NULL, 1, 0)",
        "INSERT INTO article_options (article_id, field, suffix) VALUES (10, 3, '-X')",
        "INSERT INTO article_options (article_id, field, suffix) VALUES (10, 1, '')",
    ):
        with pytest.raises(Exception):  # noqa: B017 - IntegrityError through SQLAlchemy
            with old_db.begin() as conn:
                _insert(conn, bad)


def test_downgrade_without_assembled_articles_gives_the_old_table_back(old_db):
    before = _rows(old_db, f"SELECT {ARTICLE_COLUMNS} FROM articles ORDER BY id")
    upgrade(old_db, revision=REVISION)
    downgrade(old_db, BEFORE)
    with old_db.connect() as conn:
        columns = {r[1] for r in conn.execute(text("PRAGMA table_info(articles)"))}
        lines = {r[1] for r in conn.execute(text("PRAGMA table_info(order_lines)"))}
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
    assert {"kind", "sold_alone", "assembly_mode", "parent_id", "variant_key"}.isdisjoint(columns)
    assert "assembled_id" not in lines and {"article_options", "article_components"}.isdisjoint(tables)
    assert _rows(old_db, f"SELECT {ARTICLE_COLUMNS} FROM articles ORDER BY id") == before


def test_downgrade_refuses_while_assembled_articles_exist(old_db):
    upgrade(old_db, revision=REVISION)
    with old_db.begin() as conn:
        _insert(conn, "INSERT INTO articles (id, name, kind, assembly_mode) VALUES (10, 'Box', 'assembled', 'stock')")
    with pytest.raises(Exception, match="assembled"):  # noqa: B017
        downgrade(old_db, BEFORE)


def test_an_upgraded_database_has_the_structure_of_a_new_one(old_db, tmp_path):
    upgrade(old_db)
    old_db.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(tmp_path / "old.db") == fb._structure(tmp_path / "new.db")


def test_the_rebuilt_articles_table_reads_the_same_every_time(tmp_path):
    """Written out, not Alembic's batch mode, whose order of CHECKs is not stable (see 0015, 0016)."""
    texts = set()
    for i in range(4):
        engine = make_engine(tmp_path / f"run{i}.db")
        upgrade(engine)
        engine.dispose()
        con = sqlite3.connect(tmp_path / f"run{i}.db")
        try:
            texts.add(con.execute("SELECT sql FROM sqlite_master WHERE name = 'articles'").fetchone()[0])
        finally:
            con.close()
    assert len(texts) == 1, texts


def test_full_backup_of_0_3_17_restores_with_0_4_0(tmp_path):
    """A backup taken with 0.3.17 (schema 0016) with an article, a set and an open order is accepted;
    the start then runs 0017 and 0018 (tests/test_migration_0017_set_choices.py does the same)."""
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision="0016")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO articles (id, name, library_file_id, plate_id, file_name, stock, reserved)"
            " VALUES (1, 'Segment', 1, 1, 'seg.3mf', 7, 2), (2, 'Lid', 2, 1, 'lid.3mf', 3, 0)"
        ))
        conn.execute(text("INSERT INTO product_sets (id, name) VALUES (1, 'Kit')"))
        conn.execute(text("INSERT INTO set_positions (id, set_id, sort, qty) VALUES (1, 1, 0, 1)"))
        conn.execute(text("INSERT INTO set_position_options (position_id, article_id) VALUES (1, 1)"))
        conn.execute(text("INSERT INTO orders (id, reference, order_date, status) VALUES (1, 'R-1', '2026-11-03', 'open')"))
        conn.execute(text("INSERT INTO order_lines (order_id, group_no, article_id, qty) VALUES (1, 1, 1, 2)"))
    engine.dispose()
    work = tmp_path / "work"
    work.mkdir()
    created = fb.create_backup(tmp_path / "old.db", work_dir=work, password=None,
                               now=datetime(2026, 11, 11, 12, 0, tzinfo=UTC))
    manifest = fb.read_manifest(created.path)
    assert manifest.schema_revision == "0016"
    restored = work / "candidate.db"
    fb.extract_database(created.path, manifest, None, restored)
    fb.verify_database(restored, manifest, work_dir=work)
    restored_engine = make_engine(restored)
    upgrade(restored_engine)
    assert _rows(restored_engine, "SELECT id, kind, sold_alone, stock, reserved FROM articles ORDER BY id") == [
        (1, "print", 1, 7, 2), (2, "print", 1, 3, 0),
    ]
    assert _rows(restored_engine, "SELECT version_num FROM alembic_version") == [(REVISION,)]
    restored_engine.dispose()
    fresh = make_engine(tmp_path / "new.db")
    upgrade(fresh)
    fresh.dispose()
    assert fb._structure(restored) == fb._structure(tmp_path / "new.db")
