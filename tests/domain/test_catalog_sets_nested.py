"""Sets that hold other sets (Spec 5.4, 0.4.3 p423): availability, rows of the list, saving."""

from datetime import datetime

import pytest

from factories import make_article, make_set
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import BaseInput, ChoiceInput, ChoiceOptionInput, SetInput
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import Article, ProductSet, SetPosition, SetPositionSet

ARCHIVED = datetime(2026, 11, 12, 8, 0)


def articles(s, **stocks) -> dict[str, int]:
    return {name: make_article(s, name=name.replace("_", " ").capitalize(), stock=stock).id for name, stock in stocks.items()}


def inner(set_id, *key):
    return ("set", set_id, tuple(key))


def row_of(s, set_id):
    return catalog.set_row(s, set_id)


@pytest.fixture
def kits(db):
    """The example of Spec 5.4: basic kit = 2 holders + 1 manual, extension = 1 plate + 1 screw set,
    complete kit = both; available holder 10, manual 3, plate 5, screw set 4."""
    with db.begin() as s:
        a = articles(s, holder=10, manual=3, plate=5, screw_set=4)
        basic = make_set(s, "Basic kit", [(2, a["holder"]), (1, a["manual"])], sku="EX-GA").id
        extension = make_set(s, "Extension", [(1, a["plate"]), (1, a["screw_set"])], sku="EX-ER").id
        complete = make_set(s, "Complete kit", [(1, inner(basic)), (1, inner(extension))], sku="EX-KP").id
    return a, basic, extension, complete


# ---- reading -------------------------------------------------------------------------------


def test_spec_example_complete_kit_is_three(db, kits):  # Spec 5.4, Verfügbarkeit
    a, basic, extension, complete = kits
    with db.begin() as s:
        assert [row_of(s, i).available for i in (basic, extension, complete)] == [3, 4, 3]
        row = row_of(s, complete)
        assert (row.status, row.orderable, row.incomplete) == ("deliverable", True, None)
        assert [(b.qty, b.name, b.set_id, b.article_id) for b in row.base] == [
            (1, "Basic kit", basic, None), (1, "Extension", extension, None),
        ]
        assert row.base[0].contents == ((2, "Holder"), (1, "Manual"))
        assert row.base[0].variant_sku is None and not row.base[0].archived and not row.base[0].incomplete


def test_quantity_of_an_inner_set_multiplies(db, kits):
    a, basic, extension, _ = kits
    with db.begin() as s:
        double = make_set(s, "Double", [(2, inner(basic)), (1, inner(extension))]).id
        row = row_of(s, double)
        assert row.available == 1  # 3 manuals for 2 per piece
        assert row.base[0].contents == ((2, "Holder"), (1, "Manual"))  # per piece of the inner set


def test_inner_set_with_fixed_variant_counts_only_the_chosen_article(db):
    with db.begin() as s:
        a = articles(s, frame=5, small=4, large=0, plate=9)
        sizes = make_set(s, "Sizes", [(1, a["frame"])], [(1, [(a["small"], "-S"), (a["large"], "-L")])], sku="EX-SZ").id
        outer = make_set(s, "Bundle", [(1, inner(sizes, a["small"])), (1, a["plate"])]).id
        row = row_of(s, outer)
        assert row.available == 4 and row.orderable
        assert row.base[0].variant_sku == "EX-SZ-S" and row.base[0].key == (a["small"],)
        assert row.base[0].contents == ((1, "Frame"), (1, "Small"))
        large = make_set(s, "Bundle L", [(1, inner(sizes, a["large"])), (1, a["plate"])]).id
        assert row_of(s, large).available == 0


def test_outer_choices_on_top_of_inner_sets(db, kits):
    a, basic, _extension, _ = kits
    with db.begin() as s:
        b = articles(s, red=2, blue=1)
        outer = make_set(s, "Colour kit", [(1, inner(basic))], [(1, [(b["red"], "-R"), (b["blue"], "-B")])], sku="EX-CK").id
        row = row_of(s, outer)
        assert [(v.sku, v.available) for v in row.variants] == [("EX-CK-R", 2), ("EX-CK-B", 1)]
        assert row.available == 3


def test_reservations_of_an_inner_article_lower_the_outer_set(db, kits):
    a, _basic, _extension, complete = kits
    with db.begin() as s:
        s.get(Article, a["manual"]).reserved = 2
        assert row_of(s, complete).available == 1


def test_archived_inner_set_makes_the_outer_unorderable(db, kits):  # Spec 5.4, 5.5
    _a, basic, _extension, complete = kits
    with db.begin() as s:
        s.get(ProductSet, basic).archived_at = ARCHIVED
    with db.begin() as s:
        row = row_of(s, complete)
        assert row.base[0].archived and not row.base[1].archived
        assert (row.orderable, row.available, row.status) == (False, 0, "not_deliverable")


def test_archived_article_deep_inside_makes_the_outer_unorderable(db, kits):
    a, basic, extension, complete = kits
    with db.begin() as s:
        top = make_set(s, "Top", [(1, inner(complete)), (1, articles(s, lid=3)["lid"])]).id
        s.get(Article, a["screw_set"]).archived_at = ARCHIVED
    with db.begin() as s:
        assert not row_of(s, top).orderable and row_of(s, top).available == 0
        assert not row_of(s, complete).orderable


def test_archived_chosen_option_of_inner_variant_makes_the_outer_unorderable(db):  # review focus 3
    with db.begin() as s:
        a = articles(s, frame=5, small=4, large=3, plate=9)
        sizes = make_set(s, "Sizes", [(1, a["frame"])], [(1, [(a["small"], "-S"), (a["large"], "-L")])], sku="EX-SZ").id
        outer = make_set(s, "Bundle", [(1, inner(sizes, a["small"])), (1, a["plate"])]).id
        s.get(Article, a["small"]).archived_at = ARCHIVED
    with db.begin() as s:
        row = row_of(s, outer)
        assert not row.orderable and row.available == 0
        assert row_of(s, sizes).orderable  # the inner set keeps its other variant


def test_incomplete_inner_set_makes_the_outer_unorderable(db):  # condition A2 of the plan approval
    with db.begin() as s:
        a = articles(s, frame=5, small=4, large=3, plate=9)
        sizes = make_set(s, "Sizes", [(1, a["frame"])], [(1, [(a["small"], None), (a["large"], "-L")])], sku="EX-SZ").id
        outer = make_set(s, "Bundle", [(1, inner(sizes, a["large"])), (1, a["plate"])]).id
        row = row_of(s, outer)
        assert row.base[0].incomplete and not row.base[0].archived
        assert not row.orderable and row.incomplete is None


def test_stored_ring_shows_broken_and_does_not_hang(db):  # decision 6 of the plan
    with db.begin() as s:
        a = articles(s, x=3, y=3)
        first = make_set(s, "First", [(1, a["x"])]).id
        second = make_set(s, "Second", [(1, inner(first)), (1, a["y"])]).id
        position = SetPosition(set_id=first, sort=1, qty=1, slot=0)  # first now holds second: a ring
        s.add(position)
        s.flush()
        s.add(SetPositionSet(position_id=position.id, set_id=second))
    with db.begin() as s:
        rows = {r.name: r for r in catalog.list_sets(s)}
        assert rows["First"].incomplete == "broken" and not rows["First"].orderable
        assert rows["First"].available == 0 and rows["Second"].incomplete == "broken"


def test_sets_containing_names_direct_active_parents(db, kits):
    _a, basic, _extension, complete = kits
    with db.begin() as s:
        other = make_set(s, "Another kit", [(1, inner(basic)), (1, articles(s, lid=1)["lid"])]).id
        top = make_set(s, "Top", [(1, inner(complete)), (1, articles(s, cap=1)["cap"])]).id
        gone = make_set(s, "Gone", [(1, inner(basic)), (1, articles(s, box=1)["box"])])
        gone.archived_at = ARCHIVED
        assert catalog.sets_containing(s, basic) == ["Another kit", "Complete kit"]
        assert catalog.sets_containing(s, top) == [] and other
