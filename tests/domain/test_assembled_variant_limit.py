"""An article made of parts has at most 100 variants, as a set (Spec 5.6 "Varianten und Nummern", p450)."""

import pytest

from factories import make_article
from reprintbuddy.domain import assembled
from reprintbuddy.domain.assembled import AssembledInput, OptionInput
from reprintbuddy.domain.catalog import BaseInput
from reprintbuddy.domain.errors import ValidationError


def _input(s, first: int, second: int) -> AssembledInput:
    base, one, two = (make_article(s, name=n).id for n in ("Base", "One", "Two"))
    return AssembledInput(
        name="Many", sku="EX-M", mode="stock", base=[BaseInput(1, base)],
        fields=[
            [OptionInput(None, f"-A{k}", [BaseInput(1, one)]) for k in range(first)],
            [OptionInput(None, f"-B{k}", [BaseInput(1, two)]) for k in range(second)],
        ],
        parts={},
    )


def test_exactly_100_variants_are_saved(db):
    with db.begin() as s:
        assert assembled.create_assembled(s, _input(s, 10, 10)).id


def test_more_than_100_variants_are_refused_with_the_number(db):
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            assembled.create_assembled(s, _input(s, 11, 10))
    assert (info.value.key, info.value.params) == ("assembled.too_many_variants", {"count": 110, "max": 100})
