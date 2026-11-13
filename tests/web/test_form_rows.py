"""The row helpers the set form and the form of an article made of parts share (0.4.7, R7)."""

from __future__ import annotations

import pytest

from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.web.form_rows import article_choice, qty, row_count
from reprintbuddy.web.forms import MAX_ID


def test_row_count_takes_what_the_form_sent():
    assert row_count({"r": "7"}, "r", 3, 200) == 7


def test_row_count_caps_at_the_limit():
    # p391 for the assembled form, the same for the set form since 0.4.7: never fewer rows than were sent
    assert row_count({"r": "250"}, "r", 3, 200) == 200


@pytest.mark.parametrize("raw", ["0", "", "x", "-4"])
def test_row_count_falls_back_without_a_count(raw):
    assert row_count({"r": raw}, "r", 3, 200) == 3
    assert row_count({}, "r", 3, 200) == 3


def test_article_choice_bounds():
    assert article_choice("12", "sets.field.article") == 12
    for raw in ("0", str(MAX_ID + 1), "x", ""):
        with pytest.raises(ValidationError) as caught:
            article_choice(raw, "sets.field.article")
        assert caught.value.params["field"] == "sets.field.article"


def test_qty_bounds():
    assert (qty("1", "f"), qty("1000", "f")) == (1, 1000)
    for raw in ("0", "1001", "x"):
        with pytest.raises(ValidationError):
            qty(raw, "f")
