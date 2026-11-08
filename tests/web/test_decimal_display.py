"""Decimal numbers: a comma in the German interface, a point in the English one (v0.3.8 p58)."""

import re
from pathlib import Path

import pytest

from conftest import sm_of
from factories import make_article
from reprintbuddy import settings as st
from reprintbuddy.models import ArticleFilament
from reprintbuddy.web import routes
from reprintbuddy.web.formatting import format_decimal


@pytest.mark.parametrize(
    ("value", "digits", "lang", "expected"),
    [
        (42.5, 1, "de", "42,5"),
        (42.5, 1, "en", "42.5"),
        (42.0, 1, "de", "42,0"),
        (1057.5, 0, "de", "1058"),  # whole numbers carry no separator at all
        (0.0, 1, "de", "0,0"),
        (-3.25, 2, "de", "-3,25"),
        (-3.25, 2, "en", "-3.25"),
        (7, 1, "de", "7,0"),  # an int is fine too
        (42.5, 1, "xx", "42.5"),  # an unknown language reads like English
    ],
)
def test_format_decimal(value, digits, lang, expected):
    assert format_decimal(value, digits, lang) == expected


def _article_with_grams(client):
    with sm_of(client).begin() as s:
        art = make_article(s, name="Template A")
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=42.5, material="PLA", color_hex="#000000"))
        return art.id


def _language(client, lang):
    with sm_of(client).begin() as s:
        st.set_setting(s, "language", lang, commit=False)


def test_detail_grams_follow_the_language(ready_client):
    a = _article_with_grams(ready_client)
    en_page = ready_client.get(f"/articles/{a}").text
    assert ">42.5<" in en_page and "42,5" not in en_page
    _language(ready_client, "de")
    de_page = ready_client.get(f"/articles/{a}").text
    assert ">42,5<" in de_page and "42.5" not in de_page


def test_filament_choice_caption_follows_the_language(bb_client):
    a = _article_with_grams(bb_client)
    _language(bb_client, "de")
    page = bb_client.get(f"/articles/{a}/filaments").text
    assert "42,5 g" in page and "42.5" not in page


def test_no_decimal_format_spec_in_routes():
    """A decimal number for display goes through `format_decimal`, never through an f-string such as
    `:.1f` (that one always writes a point). `:.0f` writes a whole number and is fine."""
    folder = Path(routes.__file__).parent
    pattern = re.compile(r":\.[1-9][0-9]*f\b")
    found = [
        f"{path.name}:{number}: {line.strip()}"
        for path in sorted(folder.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert found == []


# ---- every place that shows a decimal: filament list, reorder tile, new-article form ---------------

def _use_with_history(client, fake):
    """A black PLA sort that used about 71.4 g a day (700 g down to 200 g in 7 days)."""
    from datetime import timedelta

    from factories import assign_sort, make_filament_settings, make_snapshot, make_sort
    from reprintbuddy.models import utcnow

    fake.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=100.0)
    today = utcnow().date()
    with sm_of(client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(s, sort.id, pack_grams=1000, min_order_packs=1, safety_value=2000, safety_unit="grams")
        assign_sort(s, sort)
        make_snapshot(s, today - timedelta(days=10), 1, sort.id, 700.0)
        make_snapshot(s, today - timedelta(days=3), 1, sort.id, 200.0)


@pytest.mark.parametrize(("lang", "number"), [("en", "71.4"), ("de", "71,4")])
def test_use_per_day_on_the_filament_list_and_the_reorder_tile(bb_client, fake_bambuddy, lang, number):
    _use_with_history(bb_client, fake_bambuddy)
    _language(bb_client, lang)
    assert f"{number} g" in bb_client.get("/filament").text
    reorder = bb_client.get("/reorder").text
    assert number in reorder
    other = "71,4" if lang == "en" else "71.4"
    assert other not in reorder and other not in bb_client.get("/filament").text
    assert not re.search(r"\d\.\d{4,}", reorder)  # never a raw float such as 71.42857142857143


@pytest.mark.parametrize(("lang", "number"), [("en", "42.5"), ("de", "42,5")])
def test_new_article_form_shows_the_grams_of_the_file(bb_client, lang, number):
    _language(bb_client, lang)
    page = bb_client.get("/articles/new/file/1/plate/1").text
    assert f'<td class="num">{number}</td>' in page
