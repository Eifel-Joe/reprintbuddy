from datetime import datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from reprintbuddy.models import Article, Part, ProductSet

from factories import make_article

TABLES = {
    "articles", "article_filaments", "parts", "article_parts", "product_sets",
    "set_positions", "set_position_options", "orders", "order_lines", "journal",
    "form_tokens",
}


def test_tables_exist(db):
    assert TABLES <= set(inspect(db.kw["bind"]).get_table_names())


def test_head_is_0003(db):
    with db() as s:
        assert s.execute(text("select version_num from alembic_version")).scalar() == "0003"


def test_article_defaults(db):
    with db.begin() as s:
        a = Article(name="Template A", library_file_id=1, plate_id=1, file_name="x")
        s.add(a)
        s.flush()
        s.refresh(a)
        assert a.units_per_print == 1
        assert a.min_stock == 0 and a.buffer == 0
        assert a.needs_assembly is False and a.auto_enabled is False and a.paused is False
        assert a.consecutive_failures == 0
        assert (a.stock, a.reserved, a.provisional, a.awaiting_assembly) == (0, 0, 0, 0)
        assert a.sku is None


def test_plate_unique_only_among_active(db):
    with db.begin() as s:
        first = make_article(s, name="A", library_file_id=1, plate_id=1)
        first_id = first.id
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            make_article(s, name="B", library_file_id=1, plate_id=1)
    with db.begin() as s:
        s.get(Article, first_id).archived_at = datetime(2026, 1, 1)
    with db.begin() as s:
        make_article(s, name="B", library_file_id=1, plate_id=1)
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            s.execute(text("UPDATE articles SET archived_at = NULL WHERE id = :i"), {"i": first_id})


def test_archived_at_columns(db):
    for table in ("articles", "parts", "product_sets"):
        cols = {c["name"] for c in inspect(db.kw["bind"]).get_columns(table)}
        assert "archived_at" in cols, table
    with db.begin() as s:
        p = Part(name="Insert M4")
        st = ProductSet(name="Starter Set")
        s.add_all([p, st])
        s.flush()
        assert p.archived_at is None and st.archived_at is None


def test_sku_unique_but_nullable(db):
    with db.begin() as s:
        make_article(s, name="A", sku=None)
        make_article(s, name="B", sku=None)
        make_article(s, name="C", sku="A-100")
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            make_article(s, name="D", sku="A-100")


@pytest.mark.parametrize("field", ["stock", "reserved", "provisional", "awaiting_assembly"])
def test_counters_never_negative_in_db(db, field):
    with db.begin() as s:
        a = make_article(s).id
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            s.execute(text(f"UPDATE articles SET {field} = -1 WHERE id = :i"), {"i": a})


def test_part_stock_never_negative_in_db(db):
    with db.begin() as s:
        p = Part(name="Insert M4")
        s.add(p)
        s.flush()
        pid = p.id
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            s.execute(text("UPDATE parts SET stock = -1 WHERE id = :i"), {"i": pid})


def test_status_and_actor_checked(db):
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            s.execute(
                text("INSERT INTO orders (reference, order_date, status, created_at) "
                     "VALUES ('x', '2026-01-01', 'lost', '2026-01-01')")
            )
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            s.execute(
                text("INSERT INTO journal (at, actor, kind, qty) "
                     "VALUES ('2026-01-01', 'robot', 'sale', 1)")
            )
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            s.execute(text("INSERT INTO parts (name, safety_unit) VALUES ('x', 'weeks')"))
