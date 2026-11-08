"""The default unit of a part reads "Stk." in the German interface and "pcs" in the English one (v0.3.8 p119).

The database keeps "pcs" as the default (no migration); a unit typed in by the user stays as it is. The
form shows the default in the language of the interface and takes "Stk."/"pcs" back to "pcs" when saving.
"""

import re

import pytest
from sqlalchemy import select

from conftest import csrf_of, sm_of
from factories import make_part
from reprintbuddy import settings as st
from reprintbuddy.models import Part
from reprintbuddy.web.formatting import canonical_unit, unit_label
from test_parts_pages import part_fields
from test_reorder_page import add_part, mark_form


@pytest.mark.parametrize(
    ("value", "lang", "expected"),
    [("pcs", "de", "Stk."), ("pcs", "en", "pcs"), ("Satz", "de", "Satz"), ("Satz", "en", "Satz"),
     ("g", "de", "g"), ("Stk.", "en", "Stk."), ("", "de", "")],
)
def test_unit_label(value, lang, expected):
    assert unit_label(value, lang) == expected


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("pcs", "pcs"), ("Stk.", "pcs"), ("stk.", "pcs"), (" Stk. ", "pcs"), ("PCS", "pcs"), ("Satz", "Satz"), ("Stück", "Stück")],
)
def test_canonical_unit(typed, stored):
    assert canonical_unit(typed) == stored


def _language(client, lang):
    with sm_of(client).begin() as s:
        st.set_setting(s, "language", lang, commit=False)


def _units(client):
    with sm_of(client).begin() as s:
        return [p.unit for p in s.scalars(select(Part).order_by(Part.id))]


@pytest.mark.parametrize(("lang", "shown"), [("de", "Stk."), ("en", "pcs")])
def test_parts_list_shows_the_default_unit_in_the_language(ready_client, lang, shown):
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Insert M4", stock=7)
    _language(ready_client, lang)
    page = ready_client.get("/parts").text
    assert f"7 {shown}" in page
    assert ("pcs" in page) == (lang == "en")


def test_a_unit_typed_by_the_user_is_shown_as_typed(ready_client):
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Magnets", stock=3, unit="Satz")
    for lang in ("de", "en"):
        _language(ready_client, lang)
        assert "3 Satz" in ready_client.get("/parts").text


@pytest.mark.parametrize(("lang", "shown"), [("de", "Stk."), ("en", "pcs")])
def test_new_part_form_offers_the_default_in_the_language(ready_client, lang, shown):
    _language(ready_client, lang)
    page = ready_client.get("/parts/new").text
    assert re.search(rf'<input name="unit" value="{re.escape(shown)}"', page)


def test_saving_the_shown_default_stores_pcs(ready_client):
    _language(ready_client, "de")
    response = ready_client.post("/parts/new", data=part_fields(ready_client, unit="Stk."))
    assert response.status_code == 303
    assert _units(ready_client) == ["pcs"]


def test_saving_an_own_unit_keeps_it(ready_client):
    ready_client.post("/parts/new", data=part_fields(ready_client, unit="Satz"))
    assert _units(ready_client) == ["Satz"]


def test_editing_a_part_in_german_shows_and_keeps_the_default(ready_client):
    with sm_of(ready_client).begin() as s:
        part = make_part(s, name="Insert M4", stock=5)
        pid = part.id
    _language(ready_client, "de")
    page = ready_client.get(f"/parts/{pid}/edit").text
    assert re.search(r'<input name="unit" value="Stk."', page)
    fields = {
        "csrf_token": csrf_of(ready_client, f"/parts/{pid}/edit"),
        "name": "Insert M4", "unit": "Stk.", "min_stock": "5", "pack_size": "100", "min_order_packs": "1",
        "lead_time_days": "5", "safety_value": "10", "safety_unit": "days", "supplier": "", "order_url": "",
    }
    response = ready_client.post(f"/parts/{pid}/edit", data=fields)
    assert response.status_code == 303
    assert _units(ready_client) == ["pcs"]


@pytest.mark.parametrize(("lang", "shown"), [("de", "Stk."), ("en", "pcs")])
def test_reorder_page_shows_the_unit_in_the_language(bb_client, lang, shown):
    pid = add_part(bb_client)
    _language(bb_client, lang)
    html = bb_client.get("/reorder").text
    assert f"10 {shown}" in html  # stock
    assert f"(50 {shown})" in html  # the suggestion "1 × 50 (50 …)"
    assert ("pcs" in html) == (lang == "en")
    mark_form(bb_client, "part", pid, "1")
    under_way = bb_client.get("/reorder").text
    assert f"1 × 50 {shown}" in under_way
    assert ("pcs" in under_way) == (lang == "en")


def test_no_template_prints_a_unit_without_the_filter():
    """A part's unit shown as `{{ x.unit }}` would read "pcs" in the German interface again."""
    from pathlib import Path

    import reprintbuddy.web

    found = []
    for path in sorted((Path(reprintbuddy.web.__file__).parent / "templates").glob("*.html")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            found += [f"{path.name}:{number}" for _ in re.findall(r"\{\{\s*[\w.]*\.unit\s*\}\}", line)]
    assert found == []
