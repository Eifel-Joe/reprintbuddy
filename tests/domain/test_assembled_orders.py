"""Orders of articles made of parts (Spec 6.7, 6.2; 0.4.0 p341)."""

from datetime import date

import pytest
from sqlalchemy import select

from factories import make_article, make_assembled, make_part
from reprintbuddy.domain import assembled, orders
from reprintbuddy.domain.assembled import AssembledInput, OptionInput
from reprintbuddy.domain.catalog import BaseInput
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.domain.orders import ArticleItem, AssembledItem
from reprintbuddy.models import ArticlePart, JournalEntry, OrderLine

DAY = date(2026, 11, 11)


def order(s, *items):
    return orders.create_order(s, reference="R-1", order_date=DAY, items=list(items), actor="user")


def hose(s, *, stock=0):
    ids = {n: make_article(s, name=n, stock=stock * q).id for n, q in (
        ("Segment", 12), ("Side part L", 1), ("Socket L32", 1), ("Socket L40", 1))}
    composite = make_assembled(s, name="Hose", sku="EX-H1", mode="order", base=[(ids["Segment"], 12)], fields=[[
        ("-L32", [(ids["Side part L"], 1), (ids["Socket L32"], 1)]),
        ("-L40", [(ids["Side part L"], 1), (ids["Socket L40"], 1)]),
    ]])
    keys = {v.sku: v.variant_key for v in assembled.variants(s, composite)}
    return composite, ids, keys


def lines(s, order_id):
    return [
        (r.group_no, r.article_id, r.qty, r.set_qty, r.variant_sku, r.assembled_id)
        for r in s.scalars(select(OrderLine).where(OrderLine.order_id == order_id).order_by(OrderLine.id))
    ]


def test_order_of_a_box_reserves_the_box(db):
    with db.begin() as s:
        panel = make_article(s, name="Panel A")
        lid = make_article(s, name="Lid")
        box = make_assembled(s, name="Storage box", sku="EX-B1", base=[(panel.id, 1), (lid.id, 1)])
        placed = order(s, ArticleItem(box.id, 2))
        assert box.reserved == 2 and panel.reserved == 0
        assert lines(s, placed.id) == [(1, box.id, 2, None, None, None)]


def test_order_of_a_stock_variant_reserves_the_variant(db):
    with db.begin() as s:
        side = make_article(s, name="Side part L")
        other = make_article(s, name="Side part R")
        composite = make_assembled(s, name="Bracket", sku="EX-K1", fields=[[("-L", [(side.id, 2)]), ("-R", [(other.id, 2)])]])
        variant = assembled.variants(s, composite)[1]
        order(s, ArticleItem(variant.id, 1))
        assert variant.reserved == 1 and other.reserved == 0


def test_order_of_a_hose_variant_reserves_its_parts(db):  # Teil E 2
    with db.begin() as s:
        composite, ids, keys = hose(s)
        placed = order(s, AssembledItem(composite.id, 1, keys["EX-H1-L32"]))
        assert lines(s, placed.id) == [
            (1, ids["Segment"], 12, 1, "EX-H1-L32", composite.id),
            (1, ids["Side part L"], 1, 1, "EX-H1-L32", composite.id),
            (1, ids["Socket L32"], 1, 1, "EX-H1-L32", composite.id),
        ]
        view = orders.get_order(s, placed.id)
        (group,) = view.groups
        assert (group.assembled_name, group.set_qty, group.variant_sku) == ("Hose", 1, "EX-H1-L32")
        assert not view.ready


def test_order_keeps_parts_after_bom_change(db):  # review focus 3
    with db.begin() as s:
        composite, ids, keys = hose(s, stock=1)
        placed = order(s, AssembledItem(composite.id, 1, keys["EX-H1-L32"]))
        options = assembled.options(s, composite)[0]
        clamp = make_article(s, name="Clamp", stock=0)
        assembled.update_assembled(s, composite.id, AssembledInput(
            name="Hose", sku="EX-H1", mode="order", base=[BaseInput(10, ids["Segment"]), BaseInput(1, clamp.id)],
            fields=[[OptionInput(options[0].id, "-L32", [BaseInput(1, ids["Side part L"]), BaseInput(1, ids["Socket L32"])]),
                     OptionInput(options[1].id, "-L40", [BaseInput(1, ids["Side part L"]), BaseInput(1, ids["Socket L40"])])]],
            parts={},
        ))
        assert orders.get_order(s, placed.id).ready  # the parts of the order time are all there
        orders.ship_order(s, placed.id, "user")
        segment = s.get(type(composite), ids["Segment"])
        assert (segment.stock, segment.reserved) == (0, 0)  # twelve left with the order, not ten


def test_shipping_a_hose_consumes_its_purchased_parts(db):
    with db.begin() as s:
        composite, ids, keys = hose(s, stock=1)
        clamp = make_part(s, name="Clamp", stock=5)
        s.add(ArticlePart(article_id=composite.id, part_id=clamp.id, qty_per_unit=2))
        s.flush()
        placed = order(s, AssembledItem(composite.id, 1, keys["EX-H1-L40"]))
        orders.ship_order(s, placed.id, "user")
        assert clamp.stock == 3
        consumed = s.scalars(select(JournalEntry).where(JournalEntry.part_id == clamp.id)).one()
        assert consumed.details["cause"] == "shipped_assembled"


def test_part_not_sold_alone_cannot_be_ordered(db):
    with db.begin() as s:
        side = make_article(s, name="Side part L", sold_alone=False)
        with pytest.raises(ValidationError) as err:
            order(s, ArticleItem(side.id, 1))
        assert (err.value.key, err.value.params["name"]) == ("orders.not_sold_alone", "Side part L")


def test_parent_with_choices_needs_a_variant(db):
    with db.begin() as s:
        composite, _ids, _keys = hose(s)
        with pytest.raises(ValidationError) as err:
            order(s, ArticleItem(composite.id, 1))
        assert err.value.key == "orders.assembled_on_order"
        side = make_article(s, name="A")
        other = make_article(s, name="B")
        on_stock = make_assembled(s, name="Bracket", sku="EX-K1", fields=[[("-L", [(side.id, 2)]), ("-R", [(other.id, 2)])]])
        with pytest.raises(ValidationError) as err:
            order(s, ArticleItem(on_stock.id, 1))
        assert err.value.key == "orders.pick_variant"
        with pytest.raises(ValidationError) as err:
            order(s, AssembledItem(composite.id, 1, "999"))
        assert err.value.key == "orders.choice_invalid"


def test_ready_when_all_parts_in_stock(db):
    with db.begin() as s:
        composite, ids, keys = hose(s)
        placed = order(s, AssembledItem(composite.id, 2, keys["EX-H1-L32"]))
        assert not orders.get_order(s, placed.id).ready
        for name, qty in (("Segment", 24), ("Side part L", 2), ("Socket L32", 2)):
            s.get(type(composite), ids[name]).stock = qty
        s.flush()
        assert orders.get_order(s, placed.id).ready


def test_shipping_a_hose_books_its_parts_as_built_in(db):  # p361 (Spec 5.6 "Buchen und Journal")
    """Shipping an article built on order is its assembly: each part gets a row "built in" naming the
    article and its variant, like the assembly on stock; a part also ordered on its own ships as such."""
    with db.begin() as s:
        composite, ids, keys = hose(s, stock=2)
        placed = order(s, AssembledItem(composite.id, 1, keys["EX-H1-L32"]), ArticleItem(ids["Segment"], 3))
        orders.ship_order(s, placed.id, "user")
        rows = s.scalars(
            select(JournalEntry).where(JournalEntry.order_id == placed.id, JournalEntry.part_id.is_(None),
                                       JournalEntry.kind != "order_created")
            .order_by(JournalEntry.id)
        ).all()
        got = sorted((r.kind, r.article_id, r.qty, (r.details or {}).get("into"), (r.details or {}).get("variant_sku"))
                     for r in rows)
        assert got == sorted([
            ("order_shipped", composite.id, 1, None, "EX-H1-L32"),  # the article itself, as before
            ("order_shipped", ids["Segment"], 3, None, None),  # ordered on its own
            ("built_in", ids["Segment"], 12, composite.id, "EX-H1-L32"),
            ("built_in", ids["Side part L"], 1, composite.id, "EX-H1-L32"),
            ("built_in", ids["Socket L32"], 1, composite.id, "EX-H1-L32"),
        ])
        segment = s.get(type(composite), ids["Segment"])
        assert (segment.stock, segment.reserved) == (24 - 15, 0)
