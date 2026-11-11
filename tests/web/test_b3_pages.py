"""All catalog and stock pages together (Review Focus 4: nothing breaks without Bambuddy)."""

import re
from datetime import date

import pytest

from conftest import sm_of
from factories import make_article, make_part
from reprintbuddy import settings as st
from reprintbuddy.domain import catalog, orders, stock
from reprintbuddy.domain.catalog import BaseInput, SetInput
from reprintbuddy.web.i18n import translate

PAGES = [
    "/articles", "/articles?view=archived", "/articles/new",
    "/parts", "/parts?view=archived", "/parts/new",
    "/sets", "/sets?view=archived", "/sets/new",
    "/orders", "/orders?view=closed", "/orders/new",
    "/bookings", "/journal",
]


def test_all_b3_get_pages_without_bambuddy(ready_client):  # Review Focus 4
    assert ready_client.app.state.bambuddy_factory() is None
    for page in PAGES:
        response = ready_client.get(page)
        assert response.status_code == 200, page


def test_pages_with_data_without_bambuddy(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A", stock=5, thumbnail=b"\x89PNG").id
        p = make_part(s, name="Insert M4", stock=3).id
        z = make_article(s, name="Template Z", stock=5).id
        set_id = catalog.create_set(s, SetInput("Starter Set", "S-1", [BaseInput(1, a), BaseInput(1, z)], [])).id
        order_id = orders.create_order(
            s, reference="Order 1001", order_date=date(2026, 1, 15),
            items=[orders.ArticleItem(a, 1)], actor="user",
        ).id
        stock.receive_part(s, p, 5, "user")
    for page in PAGES + [f"/articles/{a}", f"/articles/{a}/edit", f"/articles/{a}/thumbnail",
                         f"/parts/{p}/edit", f"/sets/{set_id}/edit", f"/orders/{order_id}",
                         f"/journal?article={a}", f"/journal?part={p}"]:
        assert ready_client.get(page).status_code == 200, page


def test_nav_links(ready_client):
    page = ready_client.get("/").text
    for href in ("/articles", "/sets", "/orders", "/bookings", "/parts"):
        assert f'href="{href}"' in page, href
    assert translate("en", "overview.no_articles") in page  # the overview of a fresh database (Spec 10.2)


@pytest.mark.parametrize("lang", ["en", "de"])
def test_all_b3_page_titles_follow_the_language(ready_client, lang):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", lang, commit=False)
    titles = {
        "/articles": "articles.title", "/parts": "parts.title", "/sets": "sets.title",
        "/orders": "orders.title", "/bookings": "bookings.title", "/journal": "journal.title",
        "/articles/new": "articles.browse_title", "/parts/new": "parts.new", "/sets/new": "sets.new",
        "/orders/new": "orders.new",
    }
    for page, key in titles.items():
        html = ready_client.get(page).text
        title = re.search(r"<title>(.*?) · Reprintbuddy</title>", html).group(1)
        assert title == translate(lang, key), (page, lang, title)
        if lang == "de":
            assert title != translate("en", key) or key in ("sets.title", "journal.title"), page


def test_no_page_leaks_the_api_key(ready_client):
    for page in PAGES:
        assert "bb_testkey" not in ready_client.get(page).text, page
