"""Bookings of articles made of parts: build, sell, count (Spec 6.2, 8.3; 0.4.0 p341)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from factories import make_article, make_assembled, make_part
from reprintbuddy.domain import assembled, stock
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.models import Article, ArticlePart, JournalEntry, Part, utcnow


def box(s, *, panel_stock=5, panel_reserved=0, lid_stock=5, insert_stock=10):
    panel = make_article(s, name="Panel A", stock=panel_stock, reserved=panel_reserved)
    lid = make_article(s, name="Lid", stock=lid_stock)
    insert = make_part(s, name="Thread insert", stock=insert_stock)
    composite = make_assembled(s, name="Storage box", sku="EX-B1", base=[(panel.id, 2), (lid.id, 1)], min_stock=2)
    s.add(ArticlePart(article_id=composite.id, part_id=insert.id, qty_per_unit=4))
    s.flush()
    return composite, panel, lid, insert


def hose(s):
    segment = make_article(s, name="Segment", stock=30)
    side = make_article(s, name="Side part L", stock=3)
    socket = make_article(s, name="Socket L32", stock=2)
    clamp = make_part(s, name="Clamp", stock=5)
    composite = make_assembled(s, name="Hose", sku="EX-H1", mode="order", base=[(segment.id, 12)],
                               fields=[[("-L32", [(side.id, 1), (socket.id, 1)])]])
    s.add(ArticlePart(article_id=composite.id, part_id=clamp.id, qty_per_unit=2))
    s.flush()
    return composite, segment, side, socket, clamp


def test_build_moves_parts_into_the_variant(db):
    with db.begin() as s:
        composite, panel, lid, insert = box(s)
        assert assembled.build(s, composite.id, 2, "user") == []
        assert (composite.stock, panel.stock, lid.stock, insert.stock) == (2, 1, 3, 2)


def test_build_refuses_reserved_parts(db):  # Spec 8.3 / question 5: clip stock 5, 3 reserved, 4 needed
    with db.begin() as s:
        composite, panel, _lid, _insert = box(s, panel_stock=5, panel_reserved=3)
        with pytest.raises(ValidationError) as err:
            assembled.build(s, composite.id, 2, "user")
        assert err.value.key == "assembled.part_short"
        assert err.value.params == {"name": "Panel A", "available": 2, "needed": 4}
        assert (composite.stock, panel.stock) == (0, 5)
        assert s.scalars(select(JournalEntry)).all() == []


def test_build_takes_purchased_parts_to_zero_with_warning(db):  # Spec 8.3
    with db.begin() as s:
        composite, _panel, _lid, insert = box(s, insert_stock=5)
        shortages = assembled.build(s, composite.id, 2, "user")
        assert [(x.part_id, x.needed, x.had) for x in shortages] == [(insert.id, 8, 5)]
        assert insert.stock == 0 and composite.stock == 2


def test_build_writes_journal_on_both_sides(db):  # Teil C 12
    with db.begin() as s:
        composite, panel, lid, insert = box(s)
        assembled.build(s, composite.id, 1, "user")
        rows = s.scalars(select(JournalEntry).order_by(JournalEntry.id)).all()
        assert [(r.kind, r.article_id, r.part_id, r.qty) for r in rows] == [
            ("assembled", composite.id, None, 1),
            ("built_in", panel.id, None, 2),
            ("built_in", lid.id, None, 1),
            ("part_consumed", composite.id, insert.id, 4),
        ]
        assert rows[1].details["into"] == composite.id
        assert rows[3].details["cause"] == "assembled"


def test_build_a_variant_on_stock(db):
    with db.begin() as s:
        segment = make_article(s, name="Segment", stock=24)
        side = make_article(s, name="Side part L", stock=2)
        composite = make_assembled(s, name="Hose", sku="EX-H1", base=[(segment.id, 12)],
                                   fields=[[("-L", [(side.id, 1)]), ("-X", [(make_article(s, name="Side part X").id, 1)])]])
        variant = assembled.variants(s, composite)[0]
        assembled.build(s, variant.id, 2, "user")
        assert (variant.stock, segment.stock, side.stock, composite.stock) == (2, 0, 0, 0)
        entry = s.scalars(select(JournalEntry).where(JournalEntry.kind == "assembled")).one()
        assert entry.details["variant_sku"] == "EX-H1-L"
        with pytest.raises(ValidationError):  # the parent with choices holds no stock
            assembled.build(s, composite.id, 1, "user")


def test_sell_from_parts_books_parts_and_purchased_parts(db):  # Teil C 4
    with db.begin() as s:
        composite, segment, side, socket, clamp = hose(s)
        key = assembled.variants(s, composite)[0].variant_key
        assembled.sell(s, composite.id, key, 2, "user", "fair")
        assert (segment.stock, side.stock, socket.stock, clamp.stock) == (6, 1, 0, 1)
        own = s.scalars(select(JournalEntry).where(JournalEntry.article_id == composite.id, JournalEntry.part_id.is_(None))).one()
        assert (own.kind, own.qty, own.details["variant_sku"]) == ("sale", 2, "EX-H1-L32")


def test_backdated_assembled_sale_refused_after_part_count(db):  # review focus 4
    with db.begin() as s:
        composite, segment, side, _socket, _clamp = hose(s)
        key = assembled.variants(s, composite)[0].variant_key
        stock.count_article(s, side.id, "stock", 3, "user", booked_at=utcnow() - timedelta(days=1))
        before = len(s.scalars(select(JournalEntry)).all())
        with pytest.raises(ValidationError) as err:
            assembled.sell(s, composite.id, key, 1, "user", booked_at=utcnow() - timedelta(days=3))
        assert (err.value.key, err.value.params["item"]) == ("bookings.counted_later", "Side part L")
        assert len(s.scalars(select(JournalEntry)).all()) == before
        assert segment.stock == 30


def test_count_a_variant(db):
    with db.begin() as s:
        segment = make_article(s, name="Segment")
        side = make_article(s, name="Side part L")
        composite = make_assembled(s, name="Hose", sku="EX-H1", base=[(segment.id, 12)],
                                   fields=[[("-L", [(side.id, 1)]), ("-X", [(make_article(s, name="X").id, 1)])]])
        variant = assembled.variants(s, composite)[0]
        stock.count_article(s, variant.id, "stock", 4, "user")
        assert variant.stock == 4
        with pytest.raises(ValidationError) as err:
            stock.count_article(s, composite.id, "stock", 1, "user")
        assert err.value.key == "bookings.not_a_holder"


def test_order_mode_has_no_stock_to_count_or_sell_directly(db):
    with db.begin() as s:
        composite, *_ = hose(s)
        variant = assembled.variants(s, composite)[0]
        for call in (
            lambda: stock.count_article(s, variant.id, "stock", 1, "user"),
            lambda: stock.sell_unreserved(s, variant.id, 1, "user"),
        ):
            with pytest.raises(ValidationError) as err:
                call()
            assert err.value.key == "bookings.not_a_holder"


def test_stock_in_only_for_print_articles(db):  # Teil C 3
    with db.begin() as s:
        composite, panel, _lid, _insert = box(s)
        with pytest.raises(ValidationError) as err:
            stock.stock_in(s, composite.id, 1, "user", consume_parts=False)
        assert err.value.key == "bookings.print_only"
        stock.stock_in(s, panel.id, 1, "user", consume_parts=False)
        assert s.get(Article, panel.id).stock == 6
        assert s.get(Part, _insert.id).stock == 10
