"""Orders of sets that hold other sets (Spec 5.4, 6.7; 0.4.3 p423): every article of every level is
reserved, shipped and released; the order shows the articles under their inner sets as ordered. Since
0.4.4 (p441) the choice parts an inner set brings are chosen with the order."""

import json
from datetime import date, datetime

import pytest
from sqlalchemy import select

from factories import make_article, make_set
from reprintbuddy.domain import catalog, orders
from reprintbuddy.domain.catalog import BaseInput, SetInput
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.domain.orders import ArticleItem, InnerGroup, SetItem
from reprintbuddy.models import Article, OrderLine, ProductSet

TODAY = date(2026, 11, 12)
ARCHIVED = datetime(2026, 11, 12, 8, 0)


def new_order(s, items, reference="Order 2001"):
    return orders.create_order(s, reference=reference, order_date=TODAY, items=items, actor="user")


def inner(set_id):
    return ("set", set_id)


@pytest.fixture
def kits(db):
    """Basic kit EX-GA with choice parts small/large around 2 holders + 1 manual, extension = 1 plate +
    1 screw set, complete kit EX-KP = basic kit + extension: it brings small/large (EX-KP-S, EX-KP-L)."""
    with db.begin() as s:
        a = {
            name: make_article(s, name=name, stock=stock).id
            for name, stock in (("Holder", 10), ("Manual", 5), ("Small", 4), ("Large", 4), ("Plate", 5), ("Screw set", 4))
        }
        basic = make_set(s, "Basic kit", [(2, a["Holder"]), (1, a["Manual"])],
                         [(1, [(a["Small"], "-S"), (a["Large"], "-L")])], sku="EX-GA").id
        extension = make_set(s, "Extension", [(1, a["Plate"]), (1, a["Screw set"])]).id
        complete = make_set(s, "Complete kit", [(1, inner(basic)), (1, inner(extension))], sku="EX-KP").id
    return a, basic, extension, complete


def small(set_id, qty, a):
    """The complete kit (or a set around it) ordered with the small choice of the basic kit."""
    return SetItem(set_id, qty, (a["Small"],))


def reserved(s, a):
    return {name: s.get(Article, article_id).reserved for name, article_id in a.items()}


def test_order_of_a_set_of_sets_reserves_every_article(db, kits):  # Spec 5.4, 6.2
    a, _basic, _extension, complete = kits
    with db.begin() as s:
        new_order(s, [small(complete, 2, a)])
    with db.begin() as s:
        assert reserved(s, a) == {"Holder": 4, "Manual": 2, "Small": 2, "Large": 0, "Plate": 2, "Screw set": 2}


def test_order_chooses_the_brought_choice_and_keeps_both_numbers(db, kits):  # Spec 5.4, 6.7 (0.4.4)
    a, basic, _extension, complete = kits
    with db.begin() as s:
        order_id = new_order(s, [SetItem(complete, 1, (a["Large"],))]).id
    with db.begin() as s:
        assert reserved(s, a)["Large"] == 1 and reserved(s, a)["Small"] == 0
        group = orders.get_order(s, order_id).groups[0]
        assert group.variant_sku == "EX-KP-L"
        assert (group.inner[0].name, group.inner[0].variant_sku) == ("Basic kit", "EX-GA-L")


def test_inner_set_with_quantity_two_gets_the_same_choice_twice(db, kits):  # Spec 5.4
    a, basic, extension, _complete = kits
    with db.begin() as s:
        double = make_set(s, "Double", [(2, inner(basic)), (1, inner(extension))], sku="EX-DB").id
        new_order(s, [SetItem(double, 1, (a["Large"],))])
    with db.begin() as s:
        assert reserved(s, a)["Large"] == 2 and reserved(s, a)["Holder"] == 4


@pytest.mark.parametrize("key", [(), "both", "plate"])
def test_order_with_a_wrong_brought_key_is_refused(db, kits, key):
    a, _basic, _extension, complete = kits
    key = {"both": (a["Small"], a["Large"]), "plate": (a["Plate"],)}.get(key, key)
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            new_order(s, [SetItem(complete, 1, key)])
    assert info.value.key == "orders.variant_invalid"


def test_lines_keep_the_way_through_the_inner_sets(db, kits):
    a, basic, extension, complete = kits
    with db.begin() as s:
        new_order(s, [small(complete, 2, a)])
    with db.begin() as s:
        lines = s.scalars(select(OrderLine).order_by(OrderLine.id)).all()
        assert [(l.article_id, l.qty, l.set_id, l.set_qty) for l in lines][:2] == [
            (a["Holder"], 4, complete, 2), (a["Manual"], 2, complete, 2),
        ]
        assert json.loads(lines[0].inner_path) == [[basic, "EX-GA-S", 2]]
        assert json.loads(lines[3].inner_path) == [[extension, None, 2]]


def test_view_groups_by_inner_set_with_variant_and_quantity(db, kits):  # Spec 6.7
    a, _basic, _extension, complete = kits
    with db.begin() as s:
        order_id = new_order(s, [small(complete, 2, a)]).id
    with db.begin() as s:
        group = orders.get_order(s, order_id).groups[0]
        assert (group.set_name, group.set_qty, group.lines) == ("Complete kit", 2, [])
        assert group.inner == [
            InnerGroup(1, "Basic kit", "EX-GA-S", 2, [("Holder", 4), ("Manual", 2), ("Small", 2)]),
            InnerGroup(1, "Extension", None, 2, [("Plate", 2), ("Screw set", 2)]),
        ]


def test_view_three_levels_with_an_empty_middle_group(db, kits):
    a, _basic, _extension, complete = kits
    with db.begin() as s:
        lid = make_article(s, name="Lid", stock=3).id
        wrapper = make_set(s, "Wrapper", [(1, inner(complete)), (1, lid)], sku="EX-WR").id
        order_id = new_order(s, [small(wrapper, 1, a)]).id
    with db.begin() as s:
        group = orders.get_order(s, order_id).groups[0]
        assert group.lines == [("Lid", 1)]  # the articles directly in the ordered set come first
        assert [(g.depth, g.name, g.qty, g.lines) for g in group.inner] == [
            (1, "Complete kit", 1, []),
            (2, "Basic kit", 1, [("Holder", 2), ("Manual", 1), ("Small", 1)]),
            (2, "Extension", 1, [("Plate", 1), ("Screw set", 1)]),
        ]


def test_ship_and_cancel_a_set_of_sets(db, kits):  # Spec 6.2
    a, _basic, _extension, complete = kits
    with db.begin() as s:
        shipped = new_order(s, [small(complete, 1, a)]).id
        cancelled = new_order(s, [small(complete, 1, a)], reference="Order 2002").id
    with db.begin() as s:
        orders.ship_order(s, shipped, "user")
        orders.cancel_order(s, cancelled, "user")
    with db.begin() as s:
        assert reserved(s, a) == {name: 0 for name in a}
        assert s.get(Article, a["Holder"]).stock == 8 and s.get(Article, a["Small"]).stock == 3


def test_order_keeps_its_articles_when_the_inner_set_changes_later(db, kits):  # review focus 1
    a, basic, extension, complete = kits
    with db.begin() as s:
        order_id = new_order(s, [small(complete, 1, a)]).id
    with db.begin() as s:
        catalog.update_set(s, extension, SetInput("Extension XL", None, [BaseInput(3, a["Plate"]), BaseInput(1, a["Screw set"])], []))
    with db.begin() as s:
        view = orders.get_order(s, order_id)
        assert view.groups[0].inner[1].lines == [("Plate", 1), ("Screw set", 1)] and view.ready
        orders.ship_order(s, order_id, "user")
    with db.begin() as s:
        assert s.get(Article, a["Plate"]).stock == 4


def test_order_ships_after_the_inner_set_is_archived(db, kits):  # review focus 1, Spec 5.5
    a, basic, _extension, complete = kits
    with db.begin() as s:
        order_id = new_order(s, [small(complete, 1, a)]).id
        catalog.archive_set(s, complete)  # first the set that holds it (Spec 5.5, 0.4.3)
        catalog.archive_set(s, basic)
    with db.begin() as s:
        assert orders.get_order(s, order_id).groups[0].inner[0].name == "Basic kit"
        orders.ship_order(s, order_id, "user")


def test_set_with_archived_inner_set_is_ordered_and_reserves_it(db, kits):  # Spec 5.4 Altfall (0.4.3)
    a, basic, _extension, complete = kits
    with db.begin() as s:
        s.get(ProductSet, basic).archived_at = ARCHIVED
    with db.begin() as s:
        new_order(s, [small(complete, 1, a)])
    with db.begin() as s:
        assert reserved(s, a)["Holder"] == 2


def test_set_with_archived_article_deep_inside_is_ordered_and_reserves_it(db, kits):  # review focus 3, Altfall
    a, _basic, _extension, complete = kits
    with db.begin() as s:
        s.get(Article, a["Small"]).archived_at = ARCHIVED
    with db.begin() as s:
        new_order(s, [small(complete, 1, a)])
    with db.begin() as s:
        assert reserved(s, a)["Small"] == 1


def test_set_with_incomplete_inner_set_cannot_be_ordered(db, kits):  # condition A2
    a, basic, _extension, _complete = kits
    with db.begin() as s:
        lonely = make_set(s, "Lonely", [(1, a["Plate"])]).id  # too few articles: incomplete
        outer = make_set(s, "Outer", [(1, inner(lonely)), (1, a["Holder"])]).id
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            new_order(s, [SetItem(outer, 1)])
    assert info.value.key == "orders.set_incomplete"


def test_old_order_without_path_shows_as_before(db, kits):
    a, _basic, extension, _complete = kits
    with db.begin() as s:
        order_id = new_order(s, [SetItem(extension, 1), ArticleItem(a["Holder"], 1)]).id
    with db.begin() as s:
        groups = orders.get_order(s, order_id).groups
        assert groups[0].lines == [("Plate", 1), ("Screw set", 1)] and groups[0].inner == []
        assert groups[1].lines == [("Holder", 1)] and groups[1].inner == []


def test_quantities_multiply_through_two_levels_in_the_order(db, kits):  # review finding 4
    a, basic, extension, _complete = kits
    with db.begin() as s:
        lid = make_article(s, name="Lid", stock=99).id
        for article_id in a.values():
            s.get(Article, article_id).stock = 99
        complete = make_set(s, "Complete 3", [(1, inner(basic)), (3, inner(extension))], sku="EX-K3").id
        wrapper = make_set(s, "Wrapper", [(2, inner(complete)), (1, lid)], sku="EX-WR").id
        order_id = new_order(s, [small(wrapper, 3, a)]).id
    with db.begin() as s:
        group = orders.get_order(s, order_id).groups[0]
        assert group.lines == [("Lid", 3)]
        assert [(g.depth, g.name, g.qty, g.lines) for g in group.inner] == [
            (1, "Complete 3", 6, []),
            (2, "Basic kit", 6, [("Holder", 12), ("Manual", 6), ("Small", 6)]),
            (2, "Extension", 18, [("Plate", 18), ("Screw set", 18)]),
        ]
        assert reserved(s, a)["Plate"] == 18 and s.get(Article, lid).reserved == 3
