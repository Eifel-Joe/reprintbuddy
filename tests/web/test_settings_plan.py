"""Settings → General, card "Print plan": the reserve of the reload note (Spec 10.2 "Reserve", p720)."""

from __future__ import annotations

import re

import pytest

from conftest import csrf_of, sm_of
from factories import make_sort
from reprintbuddy import settings as st
from reprintbuddy.models import Setting
from reprintbuddy.web.i18n import translate


def field(page: str, name: str) -> str:
    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
    assert match, f"no field {name}"
    return match.group(0)


def stored(client) -> tuple[object, object]:
    with sm_of(client)() as s:
        return st.get_setting(s, "plan_reserve"), st.get_setting(s, "plan_reserve_grams")


def save(client, **fields):
    data = {"csrf_token": csrf_of(client, "/settings")}
    data.update(fields)
    return client.post("/settings/plan", data=data)


def test_defaults_without_a_row_in_the_database(session):
    """An update from 1.2.3 has no row: the reserve is on, with 30 g (no migration)."""
    assert session.get(Setting, "plan_reserve") is None and session.get(Setting, "plan_reserve_grams") is None
    assert st.get_setting(session, "plan_reserve") is True
    assert st.get_setting(session, "plan_reserve_grams") == 30


def test_the_card_is_on_the_general_tab_with_the_defaults(ready_client):
    page = ready_client.get("/settings?tab=general").text
    assert 'id="plan-reserve-card"' in page
    assert translate("en", "settings.plan_title") in page
    assert " checked" in field(page, "plan_reserve")
    assert 'value="30"' in field(page, "plan_reserve_grams")
    assert 'min="1"' in field(page, "plan_reserve_grams") and 'max="500"' in field(page, "plan_reserve_grams")
    assert 'action="/settings/plan"' in page


def test_the_card_is_not_on_other_tabs_nor_on_the_filament_pages(ready_client):
    assert 'id="plan-reserve-card"' not in ready_client.get("/settings?tab=automation").text
    assert 'name="plan_reserve' not in ready_client.get("/filament").text
    with sm_of(ready_client).begin() as s:
        sort_id = make_sort(s).id
    assert 'name="plan_reserve' not in ready_client.get(f"/filament/{sort_id}").text


@pytest.mark.parametrize("grams", ["1", "500", " 40 "])
def test_saves_whole_numbers_from_1_to_500(ready_client, grams):
    response = save(ready_client, plan_reserve="on", plan_reserve_grams=grams)
    assert response.status_code == 303 and response.headers["location"] == "/settings?tab=general"
    assert stored(ready_client) == (True, int(grams))


def test_switch_off_is_saved(ready_client):
    assert save(ready_client, plan_reserve_grams="30").status_code == 303
    assert stored(ready_client) == (False, 30)
    page = ready_client.get("/settings?tab=general").text
    assert " checked" not in field(page, "plan_reserve")


def test_switch_off_with_an_empty_field_keeps_the_grams(ready_client):
    save(ready_client, plan_reserve="on", plan_reserve_grams="45")
    assert save(ready_client, plan_reserve_grams="").status_code == 303
    assert stored(ready_client) == (False, 45)


@pytest.mark.parametrize("grams", ["0", "501", "2.5", "30,5", "abc", "", "-5"])
def test_wrong_grams_are_refused_on_the_general_tab(ready_client, grams):
    response = save(ready_client, plan_reserve="on", plan_reserve_grams=grams)
    assert response.status_code == 200
    page = response.text
    assert translate("en", "settings.invalid_plan_reserve_grams") in page
    card = page[page.index('id="plan-reserve-card"'):]
    card = card[:card.index("</section>")]
    assert 'role="alert"' in card  # the message stands in the card
    assert 'aria-current="page"' in page and 'href="/settings?tab=general" aria-current="page"' in page
    assert f'value="{grams}"' in field(page, "plan_reserve_grams")  # what was typed stays
    assert stored(ready_client) == (True, 30)  # nothing saved


def test_wrong_grams_are_refused_with_the_switch_off_too(ready_client):
    response = save(ready_client, plan_reserve_grams="0")
    assert response.status_code == 200
    assert translate("en", "settings.invalid_plan_reserve_grams") in response.text
    assert stored(ready_client) == (True, 30)


def test_german_message(ready_client):
    save(ready_client, plan_reserve="on", plan_reserve_grams="30")
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = save(ready_client, plan_reserve="on", plan_reserve_grams="501").text
    assert "Die Reserve muss eine ganze Zahl von 1 bis 500 Gramm sein." in page
