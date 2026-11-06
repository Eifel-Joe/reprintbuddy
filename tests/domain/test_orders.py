from datetime import date

import pytest
from sqlalchemy import select

from factories import make_article
from reprintbuddy.domain import catalog, orders, stock
from reprintbuddy.domain.catalog import PositionInput, SetInput
from reprintbuddy.domain.errors import ConflictError, NotFoundError, StockLimitError, ValidationError
from reprintbuddy.domain.orders import ArticleItem, SetItem
from reprintbuddy.models import Article, JournalEntry, Order, OrderLine

TODAY = date(2026, 1, 15)


def new_order(s, items, reference="Order 1001"):
    return orders.create_order(s, reference=reference, order_date=TODAY, items=items, actor="user")


def starter_set(s, a, b):
    """Template A x 1 and Template B x 2."""
    return catalog.create_set(
        s, SetInput("Starter Set", None, [PositionInput(1, [a]), PositionInput(2, [b])])
    )


def test_create_order_reserves(db):  # Spec 6.2
    with db.begin() as s:
        a = make_article(s).id
        order = new_order(s, [ArticleItem(a, 2)])
        order_id = order.id
    with db.begin() as s:
        assert s.get(Article, a).reserved == 2
        assert s.get(Order, order_id).status == "open"
        row = s.scalars(select(JournalEntry)).one()
        assert (row.kind, row.qty, row.order_id) == ("order_created", 2, order_id)


def test_order_with_set_reserves_articles(db):  # Spec 5.4
    with db.begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        set_id = starter_set(s, a, b).id
        new_order(s, [SetItem(set_id, 2, {})])
    with db.begin() as s:
        assert (s.get(Article, a).reserved, s.get(Article, b).reserved) == (2, 4)
        lines = s.scalars(select(OrderLine).order_by(OrderLine.id)).all()
        assert [(l.set_id, l.set_qty, l.qty) for l in lines] == [(set_id, 2, 2), (set_id, 2, 4)]
        assert {l.group_no for l in lines} == {1}


def test_choice_required(db):
    with db.begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a, b])])).id
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            new_order(s, [SetItem(set_id, 1, {})])
    assert info.value.key == "orders.choice_required"
    with db.begin() as s:
        assert s.get(Article, a).reserved == 0 and s.scalars(select(Order)).all() == []


def test_choice_must_be_allowed(db):
    with db.begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        c = make_article(s, name="Template C").id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [b, c])])).id
        position = catalog.list_sets(s)[0].positions[0].id
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            new_order(s, [SetItem(set_id, 1, {position: a})])
    assert info.value.key == "orders.choice_invalid"


def test_choice_groups_and_quantities(db):
    with db.begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        c = make_article(s, name="Template C").id
        set_id = catalog.create_set(
            s, SetInput("Starter Set", None, [PositionInput(1, [a]), PositionInput(2, [b, c])])
        ).id
        position = catalog.list_sets(s)[0].positions[1].id
        order_id = new_order(s, [SetItem(set_id, 3, {position: c}), ArticleItem(a, 1)]).id
    with db.begin() as s:
        assert (s.get(Article, a).reserved, s.get(Article, b).reserved, s.get(Article, c).reserved) == (4, 0, 6)
        view = orders.get_order(s, order_id)
        assert [g.set_name for g in view.groups] == ["Starter Set", None]
        assert view.groups[0].set_qty == 3
        assert view.groups[0].lines == [("Template A", 3), ("Template C", 6)]
        assert view.groups[1].lines == [("Template A", 1)]


def test_same_article_direct_and_via_set_aggregated(db):
    with db.begin() as s:
        a = make_article(s, name="Template A").id
        set_id = catalog.create_set(s, SetInput("S", None, [PositionInput(1, [a])])).id
        new_order(s, [ArticleItem(a, 1), SetItem(set_id, 1, {})])
    with db.begin() as s:
        assert s.get(Article, a).reserved == 2
        assert len(s.scalars(select(JournalEntry)).all()) == 1  # one booking per article


def test_ready_when_all_in_stock(db):  # Spec 6.7
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=2).id
        b = make_article(s, name="Template B", stock=3).id
        set_id = starter_set(s, a, b).id
        order_id = new_order(s, [SetItem(set_id, 2, {})]).id  # needs A 2 and B 4
    with db.begin() as s:
        assert orders.get_order(s, order_id).ready is False
        stock.stock_in(s, b, 1, "user", consume_parts=False)
    with db.begin() as s:
        assert orders.get_order(s, order_id).ready is True


def test_ship_whole_order(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=2).id
        b = make_article(s, name="Template B", stock=4).id
        order_id = new_order(s, [ArticleItem(a, 2), ArticleItem(b, 3)]).id
    with db.begin() as s:
        orders.ship_order(s, order_id, "user")
    with db.begin() as s:
        art_a, art_b = s.get(Article, a), s.get(Article, b)
        assert (art_a.stock, art_a.reserved, art_b.stock, art_b.reserved) == (0, 0, 1, 0)
        order = s.get(Order, order_id)
        assert order.status == "shipped" and order.closed_at is not None
        shipped = s.scalars(select(JournalEntry).where(JournalEntry.kind == "order_shipped")).all()
        assert len(shipped) == 2


def test_ship_not_ready_rejected(db):
    with db.begin() as s:
        a = make_article(s, stock=1).id
        order_id = new_order(s, [ArticleItem(a, 2)]).id
    with pytest.raises(StockLimitError) as info:
        with db.begin() as s:
            orders.ship_order(s, order_id, "user")
    assert info.value.key == "orders.not_ready"
    with db.begin() as s:
        art = s.get(Article, a)
        assert (art.stock, art.reserved) == (1, 2)
        assert s.get(Order, order_id).status == "open"
        assert len(s.scalars(select(JournalEntry)).all()) == 1


def test_cancel_releases(db):  # Spec 6.2
    with db.begin() as s:
        a = make_article(s, stock=5).id
        order_id = new_order(s, [ArticleItem(a, 2)]).id
    with db.begin() as s:
        orders.cancel_order(s, order_id, "user")
    with db.begin() as s:
        art = s.get(Article, a)
        assert (art.reserved, art.stock) == (0, 5)
        order = s.get(Order, order_id)
        assert order.status == "cancelled" and order.closed_at is not None


def test_closed_order_cannot_change(db):
    with db.begin() as s:
        a = make_article(s, stock=5).id
        cancelled = new_order(s, [ArticleItem(a, 1)], "Order 1").id
        shipped = new_order(s, [ArticleItem(a, 1)], "Order 2").id
    with db.begin() as s:
        orders.cancel_order(s, cancelled, "user")
        orders.ship_order(s, shipped, "user")
    for call in (
        lambda s: orders.ship_order(s, cancelled, "user"),
        lambda s: orders.cancel_order(s, shipped, "user"),
        lambda s: orders.ship_order(s, shipped, "user"),
        lambda s: orders.cancel_order(s, cancelled, "user"),
    ):
        with pytest.raises(ConflictError) as info:
            with db.begin() as s:
                call(s)
        assert info.value.key == "orders.not_open"
    with db.begin() as s:
        art = s.get(Article, a)
        assert (art.stock, art.reserved) == (4, 0)


def test_unknown_order(db):
    for call in (orders.get_order, lambda s, i: orders.ship_order(s, i, "user")):
        with pytest.raises(NotFoundError) as info:
            with db.begin() as s:
                call(s, 999)
        assert info.value.key == "orders.not_found"


def test_set_changed_after_order(db):  # Review Focus 5
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=5).id
        c = make_article(s, name="Template C", stock=5).id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a, c])])).id
        position = catalog.list_sets(s)[0].positions[0].id
        order_id = new_order(s, [SetItem(set_id, 1, {position: c})]).id
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("Starter Set", None, [PositionInput(1, [a])]))
    with db.begin() as s:
        orders.ship_order(s, order_id, "user")
    with db.begin() as s:
        art_c = s.get(Article, c)
        assert (art_c.stock, art_c.reserved) == (4, 0)
        assert s.get(Article, a).stock == 5


def test_reserved_matches_open_orders(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=10).id
        b = make_article(s, name="Template B", stock=10).id
        o1 = new_order(s, [ArticleItem(a, 2)], "Order 1").id
        o2 = new_order(s, [ArticleItem(a, 1), ArticleItem(b, 3)], "Order 2").id
        o3 = new_order(s, [ArticleItem(b, 1)], "Order 3").id
    with db.begin() as s:
        orders.cancel_order(s, o2, "user")
        orders.ship_order(s, o1, "user")
    with db.begin() as s:
        expected = orders.reserved_by_open_orders(s)
        for article in s.scalars(select(Article)):
            assert article.reserved == expected.get(article.id, 0)
        assert expected == {b: 1}


def test_order_validation(db):
    with db.begin() as s:
        a = make_article(s).id
    cases = [
        (dict(reference="  ", items=[ArticleItem(a, 1)]), "orders.reference_required"),
        (dict(reference="Order 1001", items=[]), "orders.empty"),
        (dict(reference="Order 1001", items=[ArticleItem(a, 0)]), "form.invalid_number"),
        (dict(reference="Order 1001", items=[ArticleItem(a, 1_000_001)]), "form.invalid_number"),
        (dict(reference="x" * 101, items=[ArticleItem(a, 1)]), "form.too_long"),
    ]
    for kwargs, key in cases:
        with pytest.raises(ValidationError) as info:
            with db.begin() as s:
                orders.create_order(s, order_date=TODAY, actor="user", **kwargs)
        assert info.value.key == key, kwargs
    with pytest.raises(NotFoundError):
        with db.begin() as s:
            new_order(s, [ArticleItem(999, 1)])
    with db.begin() as s:
        assert s.scalars(select(Order)).all() == []


def test_list_orders(db):
    with db.begin() as s:
        a = make_article(s, stock=9).id
        later = orders.create_order(
            s, reference="Order 2", order_date=date(2026, 2, 1), items=[ArticleItem(a, 1)], actor="user"
        ).id
        earlier = orders.create_order(
            s, reference="Order 1", order_date=date(2026, 1, 1), items=[ArticleItem(a, 1)], actor="user"
        ).id
        third = new_order(s, [ArticleItem(a, 1)], "Order 3").id
    with db.begin() as s:
        assert [o.reference for o in orders.list_orders(s, "open")] == ["Order 1", "Order 3", "Order 2"]
        orders.ship_order(s, earlier, "user")
        orders.cancel_order(s, third, "user")
    with db.begin() as s:
        assert [o.reference for o in orders.list_orders(s, "open")] == ["Order 2"]
        closed = orders.list_orders(s, "closed")
        assert [o.reference for o in closed] == ["Order 3", "Order 1"]
        assert {o.status for o in closed} == {"shipped", "cancelled"}


def test_display_status_follows_the_rules(db):  # Spec 10.1: open yellow, ready green, closed grey
    with db.begin() as s:
        a = make_article(s, stock=1).id
        waiting = new_order(s, [ArticleItem(a, 2)], reference="Order 1001").id
        ready = new_order(s, [ArticleItem(a, 1)], reference="Order 1002").id
        cancelled = new_order(s, [ArticleItem(a, 1)], reference="Order 1003").id
        orders.cancel_order(s, cancelled, "user")
    with db.begin() as s:
        assert orders.get_order(s, waiting).display_status == "open"
        assert orders.get_order(s, ready).display_status == "ready"
        assert orders.get_order(s, cancelled).display_status == "cancelled"
