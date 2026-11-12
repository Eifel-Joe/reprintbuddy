"""A set has at most 100 variants (Spec 5.4 "Mitgebrachte Wahlteile", p450): saving refuses what would
give more and names the number; through a change of an inner set the message names the set it is in."""

import pytest

from factories import make_article, make_set
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import BaseInput, ChoiceInput, ChoiceOptionInput, SetInput
from reprintbuddy.domain.errors import ValidationError


def _options(s, prefix, n):
    return [ChoiceOptionInput(make_article(s, name=f"{prefix} {k}").id, f"-{prefix}{k}") for k in range(n)]


def _refused(db, call):
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            call(s)
    return info.value


def test_exactly_100_variants_are_saved(db):
    with db.begin() as s:
        base = make_article(s, name="Base").id
        data = SetInput("Big", "EX-BIG", [BaseInput(1, base)],
                        [ChoiceInput(1, _options(s, "A", 10)), ChoiceInput(1, _options(s, "B", 10))])
        assert catalog.create_set(s, data).id


def test_more_than_100_variants_are_refused_with_the_number(db):
    with db.begin() as s:
        base = make_article(s, name="Base").id
        data = SetInput("Big", "EX-BIG", [BaseInput(1, base)],
                        [ChoiceInput(1, _options(s, "A", 11)), ChoiceInput(1, _options(s, "B", 10))])
    error = _refused(db, lambda s: catalog.create_set(s, data))
    assert (error.key, error.params) == ("sets.too_many_variants", {"count": 110, "max": 100})


def test_brought_fields_count_too(db):
    with db.begin() as s:
        inner = make_set(s, "Inner", [(1, make_article(s, name="Base").id)],
                         [(1, [(make_article(s, name=f"I {k}").id, f"-I{k}") for k in range(20)])], sku="EX-IN").id
        own = [ChoiceInput(1, _options(s, "O", 6))]
        data = SetInput("Outer", "EX-OUT", [BaseInput(1, None, inner)], own)
    error = _refused(db, lambda s: catalog.create_set(s, data))
    assert (error.key, error.params) == ("sets.too_many_variants", {"count": 120, "max": 100})


def test_change_of_an_inner_set_names_the_set_it_is_in(db):
    with db.begin() as s:
        base = make_article(s, name="Base").id
        inner = make_set(s, "Inner", [(1, base)],
                         [(1, [(make_article(s, name=f"I {k}").id, f"-I{k}") for k in range(10)])], sku="EX-IN").id
        make_set(s, "Outer", [(1, ("set", inner))],
                 [(1, [(make_article(s, name=f"O {k}").id, f"-O{k}") for k in range(10)])], sku="EX-OUT")
        more = _options(s, "J", 11)
        data = SetInput("Inner", "EX-IN", [BaseInput(1, base)], [ChoiceInput(1, more)])
    error = _refused(db, lambda s: catalog.update_set(s, inner, data))
    assert (error.key, error.params) == ("sets.parent_too_many_variants",
                                         {"names": [("Outer", False)], "count": 110, "max": 100})


def test_a_huge_nesting_is_refused_without_listing_its_variants(db):
    """10 inner sets with 10 options each would give 10**10 variants: counted, never listed."""
    with db.begin() as s:
        inners = [make_set(s, f"Part {n}", [(1, make_article(s, name=f"Base {n}").id)],
                           [(1, [(make_article(s, name=f"P{n} {k}").id, f"-{k}") for k in range(10)])],
                           sku=f"EX-P{n}").id for n in range(10)]
        data = SetInput("Huge", "EX-HUGE", [BaseInput(1, None, i) for i in inners], [])
    error = _refused(db, lambda s: catalog.create_set(s, data))
    assert error.params["count"] == 10**10
