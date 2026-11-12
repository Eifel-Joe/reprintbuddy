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
        assert row.base[0].variant_sku is None and not row.base[0].archived


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


def test_archived_inner_set_keeps_the_outer_orderable(db, kits):  # Spec 5.4 Altfall (0.4.3)
    _a, basic, _extension, complete = kits
    with db.begin() as s:
        s.get(ProductSet, basic).archived_at = ARCHIVED  # only an earlier state can leave it like this
    with db.begin() as s:
        row = row_of(s, complete)
        assert row.base[0].archived and not row.base[1].archived
        assert (row.orderable, row.available, row.status) == (True, 3, "deliverable")


def test_archived_article_deep_inside_keeps_the_outer_orderable(db, kits):  # Spec 5.4 Altfall
    a, basic, extension, complete = kits
    with db.begin() as s:
        top = make_set(s, "Top", [(1, inner(complete)), (1, articles(s, lid=3)["lid"])]).id
        s.get(Article, a["screw_set"]).archived_at = ARCHIVED
    with db.begin() as s:
        assert row_of(s, top).orderable and row_of(s, top).available == 3
        assert row_of(s, complete).orderable


def test_archived_chosen_option_of_inner_variant_keeps_the_outer_orderable(db):  # review focus 3, Altfall
    with db.begin() as s:
        a = articles(s, frame=5, small=4, large=3, plate=9)
        sizes = make_set(s, "Sizes", [(1, a["frame"])], [(1, [(a["small"], "-S"), (a["large"], "-L")])], sku="EX-SZ").id
        outer = make_set(s, "Bundle", [(1, inner(sizes, a["small"])), (1, a["plate"])]).id
        s.get(Article, a["small"]).archived_at = ARCHIVED
    with db.begin() as s:
        row = row_of(s, outer)
        assert row.orderable and row.available == 4
        assert [v.sku for v in row_of(s, sizes).variants] == ["EX-SZ-S", "EX-SZ-L"]  # every variant stays


def test_incomplete_inner_set_makes_the_outer_broken(db):  # plan v0.4.3, decision 6 (A2 withdrawn)
    with db.begin() as s:
        a = articles(s, frame=5, small=4, large=3, plate=9)
        sizes = make_set(s, "Sizes", [(1, a["frame"])], [(1, [(a["small"], None), (a["large"], "-L")])], sku="EX-SZ").id
        outer = make_set(s, "Bundle", [(1, inner(sizes, a["large"])), (1, a["plate"])]).id
        row = row_of(s, outer)
        assert (row.incomplete, row.orderable) == ("broken", False)


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


# ---- saving --------------------------------------------------------------------------------


def set_in(set_id, *key, qty=1):
    return BaseInput(qty, None, set_id, tuple(key))


def refused(db, data, key, cls=ValidationError, set_id=None):
    with pytest.raises(cls) as info:
        with db.begin() as s:
            if set_id is None:
                catalog.create_set(s, data)
            else:
                catalog.update_set(s, set_id, data)
    assert info.value.key == key, (info.value.key, info.value.params)
    return info.value


def basic_input(a, *extra):
    return SetInput("Basic kit", "EX-GA", [BaseInput(2, a["holder"]), BaseInput(1, a["manual"]), *extra], [])


def test_set_from_two_inner_sets_is_saved(db, kits):  # the user's case (p423)
    _a, basic, extension, _ = kits
    with db.begin() as s:
        new = catalog.create_set(s, SetInput("Workshop kit", "EX-WK", [set_in(basic), set_in(extension, qty=2)], [])).id
    with db.begin() as s:
        row = row_of(s, new)
        assert [(b.qty, b.set_id) for b in row.base] == [(1, basic), (2, extension)]
        assert row.available == 2 and row.orderable


def test_inner_set_with_its_variant_and_an_article_is_saved(db):
    with db.begin() as s:
        a = articles(s, frame=5, small=4, large=3, plate=9)
        sizes = make_set(s, "Sizes", [(1, a["frame"])], [(1, [(a["small"], "-S"), (a["large"], "-L")])], sku="EX-SZ").id
        new = catalog.create_set(s, SetInput("Bundle", None, [set_in(sizes, a["large"]), BaseInput(1, a["plate"])], [])).id
    with db.begin() as s:
        assert row_of(s, new).base[0].variant_sku == "EX-SZ-L" and row_of(s, new).available == 3


def test_one_inner_set_alone_is_too_few(db, kits):  # Spec 5.4: two of one inner set alone do not do
    _a, basic, _extension, _ = kits
    refused(db, SetInput("Double", None, [set_in(basic, qty=2)], []), "sets.too_few_articles")


def test_set_cannot_hold_itself(db, kits):
    a, basic, _extension, _ = kits
    error = refused(db, basic_input(a, set_in(basic)), "sets.ring", set_id=basic)
    assert error.params["path"] == "Basic kit → Basic kit"


def test_ring_through_an_inner_set_names_the_path(db, kits):  # Spec 5.4, changes
    a, basic, _extension, complete = kits
    error = refused(db, basic_input(a, set_in(complete)), "sets.ring", set_id=basic)
    assert error.params["path"] == "Basic kit → Complete kit → Basic kit"


def test_article_of_an_inner_set_cannot_be_a_base_part(db, kits):
    a, basic, _extension, _ = kits
    error = refused(db, SetInput("Kit", None, [set_in(basic), BaseInput(1, a["manual"])], []), "sets.article_twice")
    assert error.params["name"] == "Manual"


def test_article_of_an_inner_set_cannot_be_a_choice_option(db, kits):
    a, basic, _extension, _ = kits
    data = SetInput("Kit", "EX-K", [set_in(basic)], [ChoiceInput(1, [
        ChoiceOptionInput(a["plate"], "-P"), ChoiceOptionInput(a["holder"], "-H"),
    ])])
    assert refused(db, data, "sets.article_twice").params["name"] == "Holder"


def test_two_inner_sets_with_a_shared_article_are_refused(db, kits):  # F4 (b)
    a, basic, _extension, _ = kits
    with db.begin() as s:
        other = make_set(s, "Other", [(1, a["plate"]), (1, a["manual"])]).id
    data = SetInput("Kit", None, [set_in(basic), set_in(other)], [])
    assert refused(db, data, "sets.article_twice").params["name"] == "Manual"


def test_same_inner_set_twice_is_refused(db, kits):  # review focus 5
    _a, basic, _extension, _ = kits
    data = SetInput("Kit", None, [set_in(basic), set_in(basic, qty=2)], [])
    assert refused(db, data, "sets.set_twice").params["name"] == "Basic kit"


def test_same_inner_set_as_two_variants_is_refused(db):
    with db.begin() as s:
        a = articles(s, small=4, large=3, red=1, blue=1)
        colours = make_set(s, "Colours", [], [(1, [(a["small"], "-S"), (a["large"], "-L")]),
                                              (1, [(a["red"], "-R"), (a["blue"], "-B")])], sku="EX-C").id
    data = SetInput("Kit", None, [set_in(colours, a["small"], a["red"]), set_in(colours, a["large"], a["blue"])], [])
    assert refused(db, data, "sets.set_twice").params["name"] == "Colours"


def test_archived_or_incomplete_set_cannot_come_in(db, kits):
    a, basic, extension, _ = kits
    with db.begin() as s:
        s.get(ProductSet, basic).archived_at = ARCHIVED
        lonely = make_set(s, "Lonely", [(1, articles(s, lid=1)["lid"])]).id  # too few: incomplete
    data = SetInput("Kit", None, [set_in(basic), set_in(extension)], [])
    assert refused(db, data, "sets.set_not_allowed").params["name"] == "Basic kit"
    data = SetInput("Kit", None, [set_in(lonely), set_in(extension)], [])
    assert refused(db, data, "sets.set_not_allowed").params["name"] == "Lonely"


def test_archived_inner_set_already_there_may_stay(db, kits):  # decision 7 of the plan
    _a, basic, extension, complete = kits
    with db.begin() as s:
        s.get(ProductSet, basic).archived_at = ARCHIVED
    with db.begin() as s:
        catalog.update_set(s, complete, SetInput("Complete kit", "EX-KP2", [set_in(basic), set_in(extension)], []))
    with db.begin() as s:
        assert row_of(s, complete).sku == "EX-KP2" and row_of(s, complete).orderable  # Spec 5.4 Altfall


def test_unknown_inner_set(db, kits):
    _a, basic, _extension, _ = kits
    refused(db, SetInput("Kit", None, [set_in(basic), set_in(9999)], []), "sets.not_found", NotFoundError)


@pytest.mark.parametrize("key", [(), (9999,)])
def test_wrong_variant_key_is_refused(db, key):
    with db.begin() as s:
        a = articles(s, frame=5, small=4, large=3, plate=9)
        sizes = make_set(s, "Sizes", [(1, a["frame"])], [(1, [(a["small"], "-S"), (a["large"], "-L")])], sku="EX-SZ").id
        plate = a["plate"]
    data = SetInput("Bundle", None, [set_in(sizes, *key), BaseInput(1, plate)], [])
    assert refused(db, data, "sets.variant_invalid").params["set"] == "Sizes"


def test_change_of_inner_set_that_doubles_an_article_upstream_is_refused(db, kits):  # review focus 4
    a, basic, _extension, _ = kits
    error = refused(db, basic_input(a, BaseInput(1, a["plate"])), "sets.breaks_parent", ConflictError, set_id=basic)
    assert error.params["name"] == "Plate" and error.params["names"] == [("Complete kit", False)]


def test_doubling_two_levels_up_is_refused_too(db, kits):
    a, basic, _extension, complete = kits
    with db.begin() as s:
        lid = articles(s, lid=1)["lid"]
        make_set(s, "Top", [(1, inner(complete)), (1, lid)])
    error = refused(db, basic_input(a, BaseInput(1, lid)), "sets.breaks_parent", ConflictError, set_id=basic)
    assert error.params["names"] == [("Top", False)]


def test_archived_parent_is_checked_and_marked(db, kits):  # decision 5, condition A1
    a, basic, _extension, complete = kits
    with db.begin() as s:
        s.get(ProductSet, complete).archived_at = ARCHIVED
    error = refused(db, basic_input(a, BaseInput(1, a["plate"])), "sets.breaks_parent", ConflictError, set_id=basic)
    assert error.params["names"] == [("Complete kit", True)]


def test_inner_set_gaining_choices_drops_the_fixed_variant(db, kits):  # review focus 2
    a, basic, _extension, _ = kits
    with db.begin() as s:
        b = articles(s, red=1, blue=1)
    data = SetInput("Basic kit", "EX-GA", [BaseInput(2, a["holder"]), BaseInput(1, a["manual"])],
                    [ChoiceInput(1, [ChoiceOptionInput(b["red"], "-R"), ChoiceOptionInput(b["blue"], "-B")])])
    error = refused(db, data, "sets.variant_used", ConflictError, set_id=basic)
    assert error.params == {"names": [("Complete kit", False)], "set": "Basic kit"}


def test_removing_the_chosen_option_is_refused(db):
    with db.begin() as s:
        a = articles(s, frame=5, small=4, large=3, plate=9)
        sizes = make_set(s, "Sizes", [(1, a["frame"])], [(1, [(a["small"], "-S"), (a["large"], "-L")])], sku="EX-SZ").id
        make_set(s, "Bundle", [(1, inner(sizes, a["large"])), (1, a["plate"])])
    data = SetInput("Sizes", "EX-SZ", [BaseInput(1, a["frame"])], [ChoiceInput(1, [ChoiceOptionInput(a["small"], "-S")])])
    assert refused(db, data, "sets.variant_used", ConflictError, set_id=sizes).params["names"] == [("Bundle", False)]


def test_changing_only_a_suffix_keeps_the_fixed_variant(db):  # decision 2 of the plan
    with db.begin() as s:
        a = articles(s, frame=5, small=4, large=3, plate=9)
        sizes = make_set(s, "Sizes", [(1, a["frame"])], [(1, [(a["small"], "-S"), (a["large"], "-L")])], sku="EX-SZ").id
        bundle = make_set(s, "Bundle", [(1, inner(sizes, a["large"])), (1, a["plate"])]).id
    with db.begin() as s:
        catalog.update_set(s, sizes, SetInput("Sizes", "EX-SZ", [BaseInput(1, a["frame"])], [ChoiceInput(1, [
            ChoiceOptionInput(a["small"], "-S"), ChoiceOptionInput(a["large"], "-XL"),
        ])]))
    with db.begin() as s:
        assert row_of(s, bundle).base[0].variant_sku == "EX-SZ-XL" and row_of(s, bundle).orderable


def test_change_without_conflict_shows_in_the_outer_set_at_once(db, kits):  # Spec 5.4, changes
    a, basic, _extension, complete = kits
    with db.begin() as s:
        catalog.update_set(s, basic, SetInput("Basic kit", "EX-GA", [BaseInput(4, a["holder"]), BaseInput(1, a["manual"])], []))
    with db.begin() as s:
        assert row_of(s, complete).available == 2  # 10 holders for 4 per piece
        assert row_of(s, complete).base[0].contents == ((4, "Holder"), (1, "Manual"))


# ---- Altfall: archived parts in active sets (Spec 5.4, 0.4.3) --------------------------------


def test_archived_in_sets_names_the_set_that_holds_it_directly(db, kits):
    a, basic, extension, complete = kits
    with db.begin() as s:
        b = articles(s, red=1, blue=1)
        make_set(s, "Colours", [(1, a["plate"])], [(1, [(b["red"], "-R"), (b["blue"], "-B")])], sku="EX-CO")
        gone = make_set(s, "Gone", [(1, a["holder"]), (1, b["red"])])
        gone.archived_at = ARCHIVED  # an archived set warns about nothing
        s.get(Article, a["manual"]).archived_at = ARCHIVED  # in basic kit, deep inside complete kit
        s.get(Article, b["blue"]).archived_at = ARCHIVED  # a choice of colours
        s.get(ProductSet, extension).archived_at = ARCHIVED  # an inner set of complete kit (restored outer set)
    with db.begin() as s:
        found = [(w.set_name, w.kind, w.name) for w in catalog.archived_in_sets(s)]
        assert found == [
            ("Basic kit", "article", "Manual"),
            ("Colours", "article", "Blue"),
            ("Complete kit", "set", "Extension"),
        ]
        assert catalog.archived_in_sets(s)[0].set_id == basic


def test_nothing_archived_nothing_to_warn(db, kits):
    with db.begin() as s:
        assert catalog.archived_in_sets(s) == []


def test_restoring_a_set_whose_inner_set_was_archived_meanwhile(db, kits):  # Spec 5.4 Wiederherstellen (a)
    a, basic, extension, complete = kits
    with db.begin() as s:
        catalog.archive_set(s, complete)
        catalog.archive_set(s, extension)  # nothing active holds it any more
        catalog.archive_article(s, a["plate"])  # only the archived extension holds it
        catalog.restore_set(s, complete)  # restoring always goes
    with db.begin() as s:
        row = row_of(s, complete)
        assert (row.orderable, row.available, row.held_archived) == (True, 3, (("set", "Extension"),))
        assert [(w.set_name, w.kind, w.name) for w in catalog.archived_in_sets(s)] == [("Complete kit", "set", "Extension")]
        from reprintbuddy.domain.orders import SetItem, create_order
        from datetime import date
        create_order(s, reference="R-1", order_date=date(2026, 11, 12), items=[SetItem(complete, 1)], actor="user")
    with db.begin() as s:
        assert s.get(Article, a["plate"]).reserved == 1  # archived, inside the archived inner set, still reserved

