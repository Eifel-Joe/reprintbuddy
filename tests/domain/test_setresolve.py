"""Resolving a set through its inner sets into its articles (Spec 5.4, 0.4.3 p423). Pure, no database.

Since 0.4.4 (p441) an inner set brings its choice fields into the outer set; the choice is made when
ordering, there is no fixed variant any more."""

import pytest

from reprintbuddy.domain.setresolve import (
    Base,
    BadVariant,
    Cycle,
    Field,
    Line,
    ResolveError,
    SetDef,
    Slot,
    Step,
    Twice,
    Unknown,
    check,
    containing,
    direct_parents,
    fields,
    possible_articles,
    resolve,
    resolve_base,
    variant_number,
)

# article ids
HOLDER, MANUAL, PLATE, SCREWS, P, Q, X, Y, Z = 10, 11, 12, 13, 14, 15, 16, 17, 18


def defs(*sets: SetDef) -> dict[int, SetDef]:
    return {s.id: s for s in sets}


def art(qty, article_id):
    return Base(qty, article_id=article_id)


def inner(qty, set_id):
    return Base(qty, set_id=set_id)


def lines(found):
    return [(line.article_id, line.qty, line.path) for line in found]


GA = SetDef(1, (art(2, HOLDER), art(1, MANUAL)), (), name="Basic kit")
ER = SetDef(2, (art(1, PLATE), art(1, SCREWS)), (), name="Extension")
KP = SetDef(3, (inner(1, 1), inner(1, 2)), (), name="Complete")


def test_flat_set_resolves_to_its_rows():
    assert lines(resolve_base(defs(GA), 1)) == [(HOLDER, 2, ()), (MANUAL, 1, ())]


def test_inner_set_quantities_multiply_and_keep_the_path():
    assert lines(resolve_base(defs(GA, ER, KP), 3)) == [
        (HOLDER, 2, (Step(1, (), 1),)), (MANUAL, 1, (Step(1, (), 1),)),
        (PLATE, 1, (Step(2, (), 1),)), (SCREWS, 1, (Step(2, (), 1),)),
    ]
    double = SetDef(3, (inner(2, 1), inner(1, 2)), ())
    assert lines(resolve_base(defs(GA, ER, double), 3))[0] == (HOLDER, 4, (Step(1, (), 2),))


def test_three_levels_multiply_units_along_the_path():
    c = SetDef(3, (art(3, X), art(1, Y)), ())
    b = SetDef(2, (inner(2, 3), art(1, Z)), ())
    a = SetDef(1, (inner(2, 2), art(1, P)), ())
    found = resolve_base(defs(a, b, c), 1)
    assert found[0] == Line(X, 12, (Step(2, (), 2), Step(3, (), 4)))
    assert found[1] == Line(Y, 4, (Step(2, (), 2), Step(3, (), 4)))
    assert found[2] == Line(Z, 2, (Step(2, (), 2),))
    assert found[3] == Line(P, 1, ())


# The example of Spec 5.4 "Mitgebrachte Wahlteile" with made-up names: A has one choice field of three,
# B none; K1 = A + B has the three variants of A's field under its own number; A2 = A + a field of two,
# K2 = A2 + B has six variants, A's field first.
A = SetDef(1, (art(1, HOLDER), art(1, MANUAL)), (Field(1, (P, Q, Z), ("-P", "-Q", "-Z")),), name="A", sku="EX-A")
B = SetDef(2, (art(1, PLATE), art(1, SCREWS)), (), name="B", sku="EX-B")
K1 = SetDef(3, (inner(1, 1), inner(1, 2)), (), name="K1", sku="EX-K1")
A2 = SetDef(4, (inner(1, 1),), (Field(1, (X, Y), ("-X", "-Y")),), name="A2", sku="EX-A2")
K2 = SetDef(5, (inner(1, 4), inner(1, 2)), (), name="K2", sku="EX-K2")


def test_inner_set_brings_its_choice_fields_before_own():
    own = SetDef(6, (inner(2, 1), art(1, PLATE)), (Field(3, (X, Y), ("-X", "-Y")),), sku="EX-6")
    assert fields(defs(A, own), 6) == (
        Slot(2, (P, Q, Z), ("-P", "-Q", "-Z"), (1,)),  # the quantity of the row multiplies
        Slot(3, (X, Y), ("-X", "-Y"), ()),
    )
    assert fields(defs(A, B, K1), 3) == (Slot(1, (P, Q, Z), ("-P", "-Q", "-Z"), (1,)),)
    assert [variant_number(defs(A, B, K1), 3, (o,)) for o in (P, Q, Z)] == ["EX-K1-P", "EX-K1-Q", "EX-K1-Z"]


def test_two_levels_bring_fields_in_row_order():
    found = defs(A, B, A2, K2)
    assert fields(found, 5) == (
        Slot(1, (P, Q, Z), ("-P", "-Q", "-Z"), (4, 1)),
        Slot(1, (X, Y), ("-X", "-Y"), (4,)),
    )
    assert variant_number(found, 5, (Q, Y)) == "EX-K2-Q-Y"


def test_resolve_splits_key_over_inner_sets():
    found = defs(A, B, A2, K2)
    a2 = Step(4, (Q, Y), 1)
    a = Step(1, (Q,), 1)
    b = Step(2, (), 1)
    assert lines(resolve(found, 5, (Q, Y))) == [
        (HOLDER, 1, (a2, a)), (MANUAL, 1, (a2, a)), (Q, 1, (a2, a)), (Y, 1, (a2,)),
        (PLATE, 1, (b,)), (SCREWS, 1, (b,)),
    ]
    assert variant_number(found, 4, a2.key) == "EX-A2-Q-Y"  # the inner variant as ordered
    assert variant_number(found, 1, a.key) == "EX-A-Q"


def test_inner_qty_two_multiplies_field_qty():
    outer = SetDef(6, (inner(2, 1), art(1, PLATE)), (), sku="EX-6")
    assert lines(resolve(defs(A, outer), 6, (Z,))) == [
        (HOLDER, 2, (Step(1, (Z,), 2),)), (MANUAL, 2, (Step(1, (Z,), 2),)), (Z, 2, (Step(1, (Z,), 2),)),
        (PLATE, 1, ()),
    ]


def test_base_of_a_set_with_brought_fields_has_no_options():
    assert lines(resolve_base(defs(A, B, K1), 3)) == [
        (HOLDER, 1, (Step(1, (), 1),)), (MANUAL, 1, (Step(1, (), 1),)),
        (PLATE, 1, (Step(2, (), 1),)), (SCREWS, 1, (Step(2, (), 1),)),
    ]


def test_possible_articles_holds_every_option():
    assert possible_articles(defs(A, B, A2, K2), 5) == {HOLDER, MANUAL, P, Q, Z, X, Y, PLATE, SCREWS}


def test_resolve_with_key_adds_the_chosen_option_of_each_field():
    s = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q)), Field(3, (X, Y))))
    assert lines(resolve(defs(s), 1, (Q, X))) == [(HOLDER, 1, ()), (Q, 1, ()), (X, 3, ())]
    assert lines(resolve(defs(GA), 1)) == [(HOLDER, 2, ()), (MANUAL, 1, ())]


def test_ring_over_two_sets_is_a_cycle_with_its_path():
    a = SetDef(1, (inner(1, 2), art(1, X)), ())
    b = SetDef(2, (inner(1, 1), art(1, Y)), ())
    with pytest.raises(Cycle) as info:
        resolve_base(defs(a, b), 1)
    assert info.value.path == (1, 2, 1)


def test_set_holding_itself_is_a_cycle():
    a = SetDef(1, (inner(1, 1), art(1, X)), ())
    with pytest.raises(Cycle) as info:
        check(defs(a), 1)
    assert info.value.path == (1, 1)


def test_ring_three_deep():
    a = SetDef(1, (inner(1, 2),), ())
    b = SetDef(2, (inner(1, 3),), ())
    c = SetDef(3, (inner(1, 1), art(1, X)), ())
    with pytest.raises(Cycle) as info:
        resolve_base(defs(a, b, c), 1)
    assert info.value.path == (1, 2, 3, 1)


@pytest.mark.parametrize("key", [(), (Q,), (Q, Y, X), (HOLDER, Y), (Q, Q)])
def test_resolve_wrong_key_is_a_bad_variant(key):
    with pytest.raises(BadVariant) as info:
        resolve(defs(A, B, A2, K2), 5, key)
    assert (info.value.set_id, info.value.key) == (5, key)


def test_key_for_a_set_without_choices_is_a_bad_variant():
    with pytest.raises(BadVariant):
        resolve(defs(GA), 1, (HOLDER,))


def test_unknown_inner_set():
    outer = SetDef(2, (inner(1, 99), art(1, PLATE)), ())
    with pytest.raises(Unknown) as info:
        resolve_base(defs(outer), 2)
    assert info.value.set_id == 99


def test_check_passes_a_set_of_two_sets():
    check(defs(GA, ER, KP), 3)


def test_check_refuses_article_in_base_and_in_inner_set():
    outer = SetDef(3, (inner(1, 1), art(1, MANUAL)), ())
    with pytest.raises(Twice) as info:
        check(defs(GA, outer), 3)
    assert (info.value.article_id, info.value.set_id) == (MANUAL, None)


def test_check_refuses_article_in_two_inner_sets():
    other = SetDef(2, (art(1, PLATE), art(1, MANUAL)), ())
    with pytest.raises(Twice) as info:
        check(defs(GA, other, KP), 3)
    assert info.value.article_id == MANUAL


def test_check_refuses_article_in_inner_set_and_in_own_choice_field():
    # also when it is an option that a variant does not choose (Spec 5.4)
    outer = SetDef(3, (inner(1, 1), art(1, PLATE)), (Field(1, (P, HOLDER)),))
    with pytest.raises(Twice) as info:
        check(defs(GA, outer), 3)
    assert info.value.article_id == HOLDER


def test_check_refuses_article_deep_in_two_branches():
    c = SetDef(3, (art(1, X), art(1, Y)), ())
    b = SetDef(2, (inner(1, 3), art(1, Z)), ())
    d = SetDef(4, (art(1, P), art(1, Y)), ())
    a = SetDef(1, (inner(1, 2), inner(1, 4)), ())
    with pytest.raises(Twice) as info:
        check(defs(a, b, c, d), 1)
    assert info.value.article_id == Y


def test_check_refuses_the_same_inner_set_twice():
    variant_set = SetDef(1, (), (Field(1, (P, Q)), Field(1, (X, Y))))
    outer = SetDef(2, (inner(1, 1), inner(2, 1)), ())
    with pytest.raises(Twice) as info:
        check(defs(variant_set, outer), 2)
    assert (info.value.article_id, info.value.set_id) == (None, 1)


def test_check_refuses_the_same_set_twice_on_different_levels():
    middle = SetDef(4, (inner(1, 1), art(1, Z)), ())
    outer = SetDef(5, (inner(1, 4), inner(1, 1)), ())
    with pytest.raises(Twice):
        check(defs(GA, middle, outer), 5)


def test_unchosen_option_of_inner_set_counts_twice():
    # every option of a brought field is part of the outer set (Spec 5.4, 0.4.4); 0.4.3 allowed it
    variant_set = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q)),))
    outer = SetDef(2, (inner(1, 1), art(1, Q)), ())
    with pytest.raises(Twice) as info:
        check(defs(variant_set, outer), 2)
    assert info.value.article_id == Q
    own_field = SetDef(3, (inner(1, 1), art(1, PLATE)), (Field(1, (X, P)),))
    with pytest.raises(Twice):
        check(defs(variant_set, own_field), 3)
    check(defs(A, B, A2, K2), 5)


def test_check_refuses_an_article_twice_in_the_own_rows():
    s = SetDef(1, (art(1, HOLDER),), (Field(1, (P, HOLDER)),))
    with pytest.raises(Twice):
        check(defs(s), 1)


def test_containing_and_direct_parents():
    c = SetDef(3, (art(1, X), art(1, Y)), ())
    b = SetDef(2, (inner(1, 3), art(1, Z)), ())
    a = SetDef(1, (inner(1, 2), art(1, P)), ())
    d = SetDef(4, (inner(1, 3), art(1, Q)), ())
    found = defs(a, b, c, d)
    assert containing(found, 3) == [1, 2, 4]
    assert direct_parents(found, 3) == [2, 4]
    assert containing(found, 1) == [] and direct_parents(found, 1) == []


def test_containing_survives_a_stored_ring():
    a = SetDef(1, (inner(1, 2),), ())
    b = SetDef(2, (inner(1, 1),), ())
    assert containing(defs(a, b), 1) == [1, 2]


def test_variant_number_of_own_fields():
    s = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q), ("-A", "-B")),), sku="EX-GA")
    found = defs(s)
    assert variant_number(found, 1, (Q,)) == "EX-GA-B"
    assert variant_number(found, 1, ()) is None  # a key that is no variant has no number
    no_sku = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q), ("-A", "-B")),))
    assert variant_number(defs(no_sku), 1, (Q,)) is None
    no_suffix = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q), ("-A", None)),), sku="EX-GA")
    assert variant_number(defs(no_suffix), 1, (Q,)) is None
    assert variant_number(defs(GA), 1, ()) is None  # without choices no variant number
    no_outer_sku = SetDef(3, (inner(1, 1), inner(1, 2)), ())
    assert variant_number(defs(A, B, no_outer_sku), 3, (P,)) is None  # the outer set needs a number


def test_absurdly_deep_nesting_is_a_resolve_error_not_a_crash():  # review finding 7
    chain = {i: SetDef(i, (inner(1, i + 1), art(1, 1000 + i)), ()) for i in range(1, 2000)}
    chain[2000] = SetDef(2000, (art(1, 5000), art(1, 5001)), ())
    with pytest.raises(ResolveError):
        resolve_base(chain, 1)
    with pytest.raises(ResolveError):
        check(chain, 1)
    with pytest.raises(ResolveError):
        fields(chain, 1)
    with pytest.raises(ResolveError):
        resolve(chain, 1, ())
