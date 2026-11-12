"""Resolving a set through its inner sets into its articles (Spec 5.4, 0.4.3 p423). Pure, no database."""

import pytest

from reprintbuddy.domain.setresolve import (
    Base,
    BadVariant,
    Cycle,
    Field,
    Line,
    ResolveError,
    SetDef,
    Step,
    Twice,
    Unknown,
    check,
    containing,
    direct_parents,
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


def inner(qty, set_id, key=()):
    return Base(qty, set_id=set_id, key=tuple(key))


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


def test_fixed_variant_of_inner_set_takes_its_chosen_articles():
    variant_set = SetDef(1, (art(1, HOLDER),), (Field(2, (P, Q), ("-P", "-Q")),), sku="EX-GA")
    outer = SetDef(2, (inner(1, 1, (Q,)), art(1, PLATE)), ())
    assert lines(resolve_base(defs(variant_set, outer), 2)) == [
        (HOLDER, 1, (Step(1, (Q,), 1),)), (Q, 2, (Step(1, (Q,), 1),)), (PLATE, 1, ()),
    ]


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


@pytest.mark.parametrize("key", [(), (P, X), (HOLDER,)])
def test_wrong_key_is_a_bad_variant(key):
    variant_set = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q)),))
    outer = SetDef(2, (inner(1, 1, key), art(1, PLATE)), ())
    with pytest.raises(BadVariant) as info:
        resolve_base(defs(variant_set, outer), 2)
    assert (info.value.set_id, info.value.key) == (1, key)


def test_key_for_a_set_without_choices_is_a_bad_variant():
    outer = SetDef(2, (inner(1, 1, (HOLDER,)), art(1, PLATE)), ())
    with pytest.raises(BadVariant):
        resolve_base(defs(GA, outer), 2)
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


def test_check_refuses_the_same_inner_set_twice_even_as_two_variants():
    variant_set = SetDef(1, (), (Field(1, (P, Q)), Field(1, (X, Y))))
    outer = SetDef(2, (inner(1, 1, (P, X)), inner(1, 1, (Q, Y))), ())
    with pytest.raises(Twice) as info:
        check(defs(variant_set, outer), 2)
    assert (info.value.article_id, info.value.set_id) == (None, 1)
    same = SetDef(3, (inner(1, 1, (P, X)), inner(2, 1, (P, X))), ())
    with pytest.raises(Twice):
        check(defs(variant_set, same), 3)


def test_check_refuses_the_same_set_twice_on_different_levels():
    middle = SetDef(4, (inner(1, 1), art(1, Z)), ())
    outer = SetDef(5, (inner(1, 4), inner(1, 1)), ())
    with pytest.raises(Twice):
        check(defs(GA, middle, outer), 5)


def test_check_allows_unchosen_options_of_an_inner_set_elsewhere():
    variant_set = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q)),))
    outer = SetDef(2, (inner(1, 1, (P,)), art(1, Q)), ())
    check(defs(variant_set, outer), 2)


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


def test_variant_number_of_an_inner_variant():
    s = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q), ("-A", "-B")),), sku="EX-GA")
    found = defs(s)
    assert variant_number(found, 1, (Q,)) == "EX-GA-B"
    assert variant_number(found, 1, ()) is None  # a key that is no variant has no number
    no_sku = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q), ("-A", "-B")),))
    assert variant_number(defs(no_sku), 1, (Q,)) is None
    no_suffix = SetDef(1, (art(1, HOLDER),), (Field(1, (P, Q), ("-A", None)),), sku="EX-GA")
    assert variant_number(defs(no_suffix), 1, (Q,)) is None
    assert variant_number(defs(GA), 1, ()) is None  # without choices no variant number


def test_absurdly_deep_nesting_is_a_resolve_error_not_a_crash():  # review finding 7
    chain = {i: SetDef(i, (inner(1, i + 1), art(1, 1000 + i)), ()) for i in range(1, 2000)}
    chain[2000] = SetDef(2000, (art(1, 5000), art(1, 5001)), ())
    with pytest.raises(ResolveError):
        resolve_base(chain, 1)
    with pytest.raises(ResolveError):
        check(chain, 1)
