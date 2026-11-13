"""Inner sets bring their choice parts into the outer set (Spec 5.4 "Mitgebrachte Wahlteile", 0.4.4
p441): variants, numbers, availability, saving, and the warnings for sets the update left without a
number or with an article twice (plan v0.4.4, F2, F3, N1)."""

import time
from datetime import datetime

import pytest

from factories import make_article, make_set
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import BaseInput, ChoiceInput, ChoiceOptionInput, SetInput
from reprintbuddy.domain.errors import ConflictError, ValidationError
from reprintbuddy.models import ProductSet


def articles(s, **stocks) -> dict[str, int]:
    return {name: make_article(s, name=name.replace("_", " ").capitalize(), stock=stock).id for name, stock in stocks.items()}


def inner(set_id):
    return ("set", set_id)


def set_in(set_id, qty=1):
    return BaseInput(qty, None, set_id)


def option(article_id, suffix):
    return ChoiceOptionInput(article_id, suffix)


@pytest.fixture
def spec(db):
    """The example of Spec 5.4 with made-up names: Bath (EX-A) = basin + tap + choice plate -P / -Q /
    -Z; Power (EX-B) = socket + cable; K1 = Bath + Power; Bath 2 (EX-A2) = Bath + choice valve -X / -Y;
    K2 = Bath 2 + Power."""
    with db.begin() as s:
        a = articles(s, basin=9, tap=9, plate_p=3, plate_q=1, plate_z=0, socket=9, cable=9, valve_x=2, valve_y=5)
        bath = make_set(s, "Bath", [(1, a["basin"]), (1, a["tap"])],
                        [(1, [(a["plate_p"], "-P"), (a["plate_q"], "-Q"), (a["plate_z"], "-Z")])], sku="EX-A").id
        power = make_set(s, "Power", [(1, a["socket"]), (1, a["cable"])], sku="EX-B").id
        k1 = make_set(s, "K1", [(1, inner(bath)), (1, inner(power))], sku="EX-K1").id
        bath2 = make_set(s, "Bath 2", [(1, inner(bath))], [(1, [(a["valve_x"], "-X"), (a["valve_y"], "-Y")])], sku="EX-A2").id
        k2 = make_set(s, "K2", [(1, inner(bath2)), (1, inner(power))], sku="EX-K2").id
    return a, {"bath": bath, "power": power, "k1": k1, "bath2": bath2, "k2": k2}


def refused(db, data, key, cls=ValidationError, set_id=None):
    with pytest.raises(cls) as info:
        with db.begin() as s:
            if set_id is None:
                catalog.create_set(s, data)
            else:
                catalog.update_set(s, set_id, data)
    assert info.value.key == key, (info.value.key, info.value.params)
    return info.value


def bath_input(a, plates=(("plate_p", "-P"), ("plate_q", "-Q"), ("plate_z", "-Z")), sku="EX-A"):
    return SetInput("Bath", sku, [BaseInput(1, a["basin"]), BaseInput(1, a["tap"])],
                    [ChoiceInput(1, [option(a[name], suffix) for name, suffix in plates])] if plates else [])


# ---- reading -------------------------------------------------------------------------------


def test_outer_variants_from_brought_and_own_fields(db, spec):
    a, ids = spec
    with db.begin() as s:
        k1 = catalog.set_row(s, ids["k1"])
        assert [(v.sku, v.names, v.available) for v in k1.variants] == [
            ("EX-K1-P", ["Plate p"], 3), ("EX-K1-Q", ["Plate q"], 1), ("EX-K1-Z", ["Plate z"], 0),
        ]
        assert (k1.available, k1.orderable, k1.incomplete, k1.choices) == (4, True, None, [])
        assert [(b.name, b.sku, b.set_id) for b in k1.base] == [("Bath", "EX-A", ids["bath"]), ("Power", "EX-B", ids["power"])]
        k2 = catalog.set_row(s, ids["k2"])
        assert [v.sku for v in k2.variants] == [
            "EX-K2-P-X", "EX-K2-P-Y", "EX-K2-Q-X", "EX-K2-Q-Y", "EX-K2-Z-X", "EX-K2-Z-Y",
        ]
        assert k2.variants[3].names == ["Plate q", "Valve y"] and k2.variants[3].key == (a["plate_q"], a["valve_y"])


def test_availability_with_brought_field_and_quantity_two(db, spec):
    a, ids = spec
    with db.begin() as s:
        double = make_set(s, "Double", [(2, inner(ids["bath"])), (1, inner(ids["power"]))], sku="EX-D").id
        row = catalog.set_row(s, double)
        # two baths per piece, both with the same plate: plate p 3 -> 1, plate q 1 -> 0, plate z 0 -> 0
        assert [(v.sku, v.available) for v in row.variants] == [("EX-D-P", 1), ("EX-D-Q", 0), ("EX-D-Z", 0)]
        assert row.available == 1  # together at most: 3 // 2 + 1 // 2 + 0 // 2


# ---- saving --------------------------------------------------------------------------------


def test_set_with_brought_choices_is_saved(db, spec):
    a, ids = spec
    with db.begin() as s:
        new = catalog.create_set(s, SetInput("K3", "EX-K3", [set_in(ids["bath"]), set_in(ids["power"])], [])).id
    with db.begin() as s:
        assert [v.sku for v in catalog.set_row(s, new).variants] == ["EX-K3-P", "EX-K3-Q", "EX-K3-Z"]


def test_outer_without_number_refused_when_inner_brings_choices(db, spec):  # Spec 5.4, plan E1; since p533 for every set
    _a, ids = spec
    refused(db, SetInput("K3", None, [set_in(ids["bath"]), set_in(ids["power"])], []), "form.required")


def test_inner_gaining_choices_while_a_parent_has_no_number_is_refused(db, spec):  # plan F2
    a, ids = spec
    with db.begin() as s:
        lid = articles(s, lid=1, red=1, blue=1)
        plain = make_set(s, "Plain", [(1, a["socket"]), (1, lid["lid"])], sku="EX-PL").id
        make_set(s, "No number", [(1, inner(plain)), (1, a["cable"])])
    data = SetInput("Plain", "EX-PL", [BaseInput(1, a["socket"]), BaseInput(1, lid["lid"])],
                    [ChoiceInput(1, [option(lid["red"], "-R"), option(lid["blue"], "-B")])])
    error = refused(db, data, "sets.parent_sku_required", ConflictError, set_id=plain)
    assert error.params["names"] == [("No number", False)]


def test_inner_gaining_choices_changes_the_outer_variants_at_once(db, spec):
    a, ids = spec
    with db.begin() as s:
        red = articles(s, red=1)["red"]
        catalog.update_set(s, ids["power"], SetInput("Power", "EX-B", [BaseInput(1, a["socket"]), BaseInput(1, a["cable"])],
                                                       [ChoiceInput(1, [option(red, "-R")])]))
    with db.begin() as s:
        assert [v.sku for v in catalog.set_row(s, ids["k1"]).variants] == ["EX-K1-P-R", "EX-K1-Q-R", "EX-K1-Z-R"]


def test_removing_an_option_of_an_inner_set_is_no_longer_refused(db, spec):  # "Variante fällt weg" went
    a, ids = spec
    with db.begin() as s:
        catalog.update_set(s, ids["bath"], bath_input(a, (("plate_p", "-P"),)))
    with db.begin() as s:
        assert [v.sku for v in catalog.set_row(s, ids["k1"]).variants] == ["EX-K1-P"]
        catalog.update_set(s, ids["bath"], bath_input(a, ()))
    with db.begin() as s:
        k1 = catalog.set_row(s, ids["k1"])
        assert k1.variants == [] and k1.orderable


def test_inner_suffix_change_changes_outer_variants(db, spec):
    a, ids = spec
    with db.begin() as s:
        catalog.update_set(s, ids["bath"], bath_input(a, (("plate_p", "-P"), ("plate_q", "-QQ"), ("plate_z", "-Z"))))
    with db.begin() as s:
        assert [v.sku for v in catalog.set_row(s, ids["k1"]).variants] == ["EX-K1-P", "EX-K1-QQ", "EX-K1-Z"]
        assert catalog.set_row(s, ids["k2"]).variants[2].sku == "EX-K2-QQ-X"




def test_inner_suffix_change_that_gives_another_variants_number_is_refused(db, spec):
    a, ids = spec
    with db.begin() as s:
        other = articles(s, other=1)["other"]
        make_set(s, "Other", [(1, a["socket"])], [(1, [(other, "-Z9")])], sku="EX-K2-P")  # variant EX-K2-P-Z9
    data = SetInput("Bath 2", "EX-A2", [set_in(ids["bath"])],
                    [ChoiceInput(1, [option(a["valve_x"], "-X"), option(a["valve_y"], "-Z9")])])
    error = refused(db, data, "sets.parent_sku_taken", ConflictError, set_id=ids["bath2"])
    assert error.params == {"names": [("K2", False)], "sku": "EX-K2-P-Z9"}  # K2 = plate p of bath, then valve y




def test_article_equal_to_an_unchosen_option_of_an_inner_set_is_refused(db, spec):  # plan E4
    a, ids = spec
    data = SetInput("K3", "EX-K3", [set_in(ids["bath"]), BaseInput(1, a["plate_z"])], [])
    assert refused(db, data, "sets.article_twice").params["name"] == "Plate z"


def test_inner_option_that_an_outer_set_holds_is_refused_upstream(db, spec):
    a, ids = spec
    with db.begin() as s:
        hook = articles(s, hook=1)["hook"]
        make_set(s, "K4", [(1, inner(ids["bath"])), (1, hook)], sku="EX-K4")
    data = SetInput("Bath", "EX-A", [BaseInput(1, a["basin"]), BaseInput(1, a["tap"])],
                    [ChoiceInput(1, [option(a["plate_p"], "-P"), option(hook, "-H")])])
    error = refused(db, data, "sets.breaks_parent", ConflictError, set_id=ids["bath"])
    assert error.params["names"] == [("K4", False)] and error.params["name"] == "Hook"


def test_variant_numbers_include_brought(db, spec):
    _a, ids = spec
    with db.begin() as s:
        assert catalog.variant_numbers(s)["EX-K2-Q-Y"] == ids["k2"]
    refused(db, SetInput("Copy", "EX-K1-Q", [BaseInput(1, spec[0]["basin"]), BaseInput(1, spec[0]["tap"])], []),
            "catalog.sku_taken", ConflictError)


# ---- states the update to 0.4.4 leaves (Spec 5.4 update paragraph, plan F3 and N1) -------------


def test_number_missing_state(db, spec):
    a, ids = spec
    with db.begin() as s:
        lost = make_set(s, "Lost number", [(1, inner(ids["bath"])), (1, inner(ids["power"]))]).id  # as the update leaves it
        old = make_set(s, "Old kind", [(1, a["socket"])], [(1, [(a["cable"], "-C")])]).id  # the update to 0.4.0
        row = catalog.set_row(s, lost)
        assert (row.incomplete, row.orderable) == ("number_missing", False)
        assert catalog.set_row(s, old).incomplete == "suffixes_missing"


def test_sets_to_mend_names_number_missing_and_twice(db, spec):  # plan N1
    a, ids = spec
    with db.begin() as s:
        make_set(s, "Lost number", [(1, inner(ids["bath"])), (1, inner(ids["power"]))])
        clash = make_set(s, "Clash", [(1, inner(ids["bath"])), (1, a["plate_q"])], sku="EX-CL").id
        gone = make_set(s, "Gone", [(1, inner(ids["bath"])), (1, inner(ids["power"]))])
        gone.archived_at = datetime(2026, 11, 12, 8, 0)  # an archived set is not in the order form anyway
    with db.begin() as s:
        found = [(m.set_name, m.kind, m.name) for m in catalog.sets_to_mend(s)]
        assert found == [("Clash", "twice", "Plate q"), ("Lost number", "number_missing", None)]
        assert catalog.set_row(s, clash).incomplete == "broken"
        assert catalog.sets_to_mend(s)[0].set_id == clash


def test_nothing_to_mend(db, spec):
    with db.begin() as s:
        assert catalog.sets_to_mend(s) == []


def test_many_fields_stay_fast(db):  # review focus 1; since p450 at most 100 variants: 3 ** 4 = 81
    with db.begin() as s:
        inner_ids = []
        for n in range(4):
            a = articles(s, **{f"base_{n}": 5, f"one_{n}": 2, f"two_{n}": 2, f"three_{n}": 2})
            inner_ids.append(make_set(s, f"Part {n}", [(1, a[f"base_{n}"])], [(1, [
                (a[f"one_{n}"], "-1"), (a[f"two_{n}"], "-2"), (a[f"three_{n}"], "-3"),
            ])], sku=f"EX-P{n}").id)
        big = catalog.create_set(s, SetInput("Big", "EX-BIG", [set_in(i) for i in inner_ids], [])).id
    started = time.perf_counter()
    with db.begin() as s:
        rows = {r.name: r for r in catalog.list_sets(s)}
    assert len(rows["Big"].variants) == 81 and rows["Big"].variants[0].sku == "EX-BIG-1-1-1-1"
    assert time.perf_counter() - started < 5, "the list of sets must stay quick"
    assert big


# ---- review of v0.4.4 ----------------------------------------------------------------------


def test_deep_chain_stays_quick(db):  # review finding 1: _incomplete_of went exponential with depth
    with db.begin() as s:
        a = articles(s, **{f"x{n}": 1 for n in range(32)})
        below = make_set(s, "Level 0", [(1, a["x0"]), (1, a["x1"])]).id
        for n in range(1, 30):
            below = make_set(s, f"Level {n}", [(1, inner(below)), (1, a[f"x{n + 1}"])]).id
    started = time.perf_counter()
    with db.begin() as s:
        rows = catalog.list_sets(s)
        assert catalog.sets_to_mend(s) == []
    assert all(r.incomplete is None for r in rows)
    assert time.perf_counter() - started < 5, "a chain of 30 levels must not hang the set list"


def test_three_levels_after_the_update_show_number_missing_not_broken(db, spec):  # review finding 2
    _a, ids = spec
    with db.begin() as s:
        middle = make_set(s, "Middle", [(1, inner(ids["bath"])), (1, inner(ids["power"]))]).id  # no number
        hook = articles(s, hook=1)["hook"]
        top = make_set(s, "Top", [(1, inner(middle)), (1, hook)]).id  # no number either
        numbered = make_set(s, "Numbered", [(1, inner(middle)), (1, articles(s, cap=1)["cap"])], sku="EX-NB").id
    with db.begin() as s:
        assert catalog.set_row(s, middle).incomplete == "number_missing"
        assert catalog.set_row(s, top).incomplete == "number_missing"  # Spec 5.4 update paragraph
        row = catalog.set_row(s, numbered)
        assert row.incomplete is None and row.orderable and row.variants[0].sku == "EX-NB-P"
        assert [(m.set_name, m.kind) for m in catalog.sets_to_mend(s)] == [
            ("Middle", "number_missing"), ("Top", "number_missing"),
        ]


def test_taken_number_in_a_containing_set_names_that_set(db, spec):  # review finding 3, Spec 5.4 "Änderungen"
    a, ids = spec
    with db.begin() as s:
        make_article(s, name="Taken", sku="EX-K1-W")
    error = refused(db, bath_input(a, (("plate_p", "-P"), ("plate_q", "-W"), ("plate_z", "-Z"))),
                    "sets.parent_sku_taken", ConflictError, set_id=ids["bath"])
    assert error.params == {"names": [("K1", False)], "sku": "EX-K1-W"}


def test_too_long_number_in_a_containing_set_names_that_set(db, spec):  # review finding 3
    a, ids = spec
    with db.begin() as s:
        s.get(ProductSet, ids["k1"]).sku = "EX-" + "K" * 58
    error = refused(db, bath_input(a, (("plate_p", "-P"), ("plate_q", "-QQQ"), ("plate_z", "-Z"))),
                    "sets.parent_variant_too_long", ValidationError, set_id=ids["bath"])
    assert error.params == {"names": [("K1", False)], "sku": "EX-" + "K" * 58 + "-QQQ", "max": 64}


def test_stored_ring_inside_a_set_with_too_few_rows_shows_broken(db):  # review finding 4
    from reprintbuddy.models import SetPosition, SetPositionSet
    with db.begin() as s:
        x = articles(s, x=1)["x"]
        first = make_set(s, "First", [(1, x)]).id  # too few rows
        second = make_set(s, "Second", [(1, inner(first)), (1, articles(s, y=1)["y"])]).id
        position = SetPosition(set_id=first, sort=1, qty=1, slot=0)
        s.add(position)
        s.flush()
        s.add(SetPositionSet(position_id=position.id, set_id=second))  # first holds second: a ring
        first_alone = make_set(s, "Alone", [(1, inner(second))]).id  # one row only: too few, ring inside
    with db.begin() as s:
        rows = {r.name: r for r in catalog.list_sets(s)}  # no error page
        assert rows["Alone"].incomplete == "broken" and not rows["Alone"].orderable
        assert first_alone


def test_parent_without_number_refused_also_when_archived(db, spec):  # plan F2, archived parents count too
    a, ids = spec
    with db.begin() as s:
        lid = articles(s, lid=1, red=1)
        plain = make_set(s, "Plain", [(1, a["socket"]), (1, lid["lid"])], sku="EX-PL").id
        gone = make_set(s, "Gone", [(1, inner(plain)), (1, a["cable"])])
        gone.archived_at = datetime(2026, 11, 12, 8, 0)
    data = SetInput("Plain", "EX-PL", [BaseInput(1, a["socket"]), BaseInput(1, lid["lid"])],
                    [ChoiceInput(1, [option(lid["red"], "-R")])])
    error = refused(db, data, "sets.parent_sku_required", ConflictError, set_id=plain)
    assert error.params["names"] == [("Gone", True)]
