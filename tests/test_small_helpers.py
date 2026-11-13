"""Small helpers defined once instead of in several modules (refactoring before 1.0.0, R4)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from factories import make_article
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.domain.inputs import int_in, text
from reprintbuddy.models import Article, naive_utc
from reprintbuddy.web.deps import get_or_404
from reprintbuddy.web.forms import MAX_ID, maintenance_redirect


def test_naive_utc_converts_an_aware_time():
    assert naive_utc(datetime(2026, 1, 1, 12, tzinfo=ZoneInfo("Europe/Berlin"))) == datetime(2026, 1, 1, 11)


def test_naive_utc_keeps_a_naive_time():
    assert naive_utc(datetime(2026, 1, 1, 12)) == datetime(2026, 1, 1, 12)


@pytest.mark.parametrize("value", [True, 0, 11, "5", 5.0])
def test_int_in_refuses_bool_other_types_and_out_of_range(value):
    with pytest.raises(ValidationError) as caught:
        int_in(value, "f", 1, 10)
    assert caught.value.key == "form.invalid_number"
    assert caught.value.params == {"field": "f", "min": 1, "max": 10}


def test_int_in_takes_the_bounds():
    assert (int_in(1, "f", 1, 10), int_in(10, "f", 1, 10)) == (1, 10)


def test_text_trims_and_limits():
    assert text("  a ", "f", 5) == "a"
    assert text("   ", "f", 5) is None
    assert text(None, "f", 5) is None
    with pytest.raises(ValidationError) as caught:
        text(" ", "f", 5, required=True)
    assert (caught.value.key, caught.value.params) == ("form.required", {"field": "f"})
    with pytest.raises(ValidationError) as caught:
        text("abcdef", "f", 5)
    assert (caught.value.key, caught.value.params) == ("form.too_long", {"field": "f", "max": 5})


def test_max_id_is_the_largest_record_number_a_form_may_name():
    assert MAX_ID == 2**31 - 1


def test_get_or_404_finds_refuses_missing_and_refuses_invalid(db):
    with db.begin() as s:
        article_id = make_article(s).id
    with db() as s:
        assert get_or_404(s, Article, article_id).id == article_id
        with pytest.raises(HTTPException) as caught:
            get_or_404(s, Article, article_id + 1)
        assert caught.value.status_code == 404
        with pytest.raises(HTTPException) as caught:
            get_or_404(s, Article, article_id, valid=lambda a: a.kind == "assembled")
        assert caught.value.status_code == 404


def test_maintenance_redirect():
    plain = maintenance_redirect(None, "ops-update")
    assert (plain.status_code, plain.headers["location"]) == (303, "/settings?tab=maintenance#ops-update")
    coded = maintenance_redirect("restore_expired", "ops-restore")
    assert coded.headers["location"] == "/settings?tab=maintenance&ops=restore_expired#ops-restore"
