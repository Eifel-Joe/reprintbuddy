"""The set pages in the new look: status badges, short summary, search, filters, tiles or list and
live regions (Spec 10.1, plan Task 13)."""

import re

import pytest
from conftest import csrf_of, sm_of
from factories import make_article, make_set

from reprintbuddy import settings as st
from reprintbuddy.domain import catalog, stock
from reprintbuddy.models import ProductSet, utcnow
from reprintbuddy.web import regions


def summary_of(page):
    return re.search(r'<p class="summary">(.*?)</p>', page, re.S).group(1)


def text_of(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def region_of(page, name):
    return regions.extract(page, [name])[name]


def list_of(page):
    return region_of(page, "list")


def set_with(client, name, sku=None, **article_fields):
    """A set of one position (1 × a new article); returns (set id, article id)."""
    with sm_of(client).begin() as s:
        article = make_article(s, name=f"Part of {name}", **article_fields)
        product_set = make_set(s, name, [(1, [article.id])])
        product_set.sku = sku
        return product_set.id, article.id


def two_states(client):
    set_with(client, "Starter Set", sku="S-100", stock=2)
    set_with(client, "Big Set", sku="S-200", stock=0)


def test_set_row_has_a_status(ready_client):
    two_states(ready_client)
    with sm_of(ready_client).begin() as s:
        states = {row.name: row.status for row in catalog.list_sets(s)}
    assert states == {"Starter Set": "deliverable", "Big Set": "not_deliverable"}


@pytest.mark.parametrize(
    "stock_of_article,text,classes",
    [(1, "deliverable", "tone-ok"), (0, "not deliverable", "tone-bad")],
)
def test_set_badges(ready_client, stock_of_article, text, classes):
    set_with(ready_client, "Starter Set", stock=stock_of_article)
    assert f'<span class="badge {classes}">{text}</span>' in list_of(ready_client.get("/sets").text)


def test_sets_summary_filter_and_search(ready_client):
    two_states(ready_client)
    page = ready_client.get("/sets").text
    assert text_of(summary_of(page)) == "2 sets · 1 not deliverable"
    only = ready_client.get("/sets?status=not_deliverable").text
    assert "Big Set" in list_of(only) and "Starter Set" not in list_of(only)
    filters = region_of(only, "filters")
    assert re.search(r'class="filter active" href="/sets\?status=not_deliverable"', filters)
    assert re.search(r'>All <span class="count">2</span>', filters)
    by_number = ready_client.get("/sets?q=s-1").text
    assert "Starter Set" in list_of(by_number) and "Big Set" not in list_of(by_number)
    by_name = ready_client.get("/sets?q=BIG").text
    assert "Big Set" in list_of(by_name) and "Starter Set" not in list_of(by_name)
    nothing = ready_client.get("/sets?q=%25").text
    assert "Nothing matches the search or filter." in list_of(nothing)
    assert ready_client.get("/sets?status=bogus").status_code == 200


def test_sets_summary_in_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    two_states(ready_client)
    assert text_of(summary_of(ready_client.get("/sets").text)) == "2 Sets · 1 nicht lieferbar"


def test_sets_list_and_tiles(ready_client):
    set_id, _ = set_with(ready_client, "Starter Set", sku="S-100", stock=3)
    listing = list_of(ready_client.get("/sets").text)
    assert 'class="collection layout-auto"' in listing
    table = re.search(r"<table>.*?</table>", listing, re.S).group(0)
    assert f'<a href="/sets/{set_id}/edit">Starter Set</a>' in table and "S-100" in table
    assert '<td class="num">3</td>' in table and "1 × Part of Starter Set" in table
    tile = re.search(rf'<a class="tile" href="/sets/{set_id}/edit">.*?</a>', listing, re.S).group(0)
    assert "Starter Set" in tile and "S-100" in tile and '<span class="badge tone-ok">deliverable</span>' in tile
    assert "<strong>3</strong>" in tile
    assert re.search(r'class="tile-foot">.*1 × Part of Starter Set', tile, re.S)


def test_sets_head_and_buttons(ready_client):
    page = ready_client.get("/sets").text
    assert 'class="page-head"' in page and "#boxes" in page
    assert re.search(r'<a class="btn" href="/sets/new">.*?#plus.*?New set</a>', page, re.S)
    assert re.search(r'<a class="btn secondary" href="/sets\?view=archived">.*?#archive', page, re.S)
    archive = ready_client.get("/sets?view=archived").text
    assert re.search(r'<a class="btn secondary" href="/sets">.*?#archive.*?Show active', archive, re.S)


def test_archived_sets_show_the_archived_badge_and_no_filters(ready_client):
    set_id, _ = set_with(ready_client, "Old Set", stock=0)
    with sm_of(ready_client).begin() as s:
        s.get(ProductSet, set_id).archived_at = utcnow()
    page = ready_client.get("/sets?view=archived").text
    assert '<span class="badge tone-neutral">Archived</span>' in list_of(page)
    assert "not deliverable" not in list_of(page)
    assert 'data-live-region="filters"' not in page
    assert text_of(summary_of(page)) == "1 set"


def test_sets_view_choice_is_its_own(ready_client):
    two_states(ready_client)
    ready_client.cookies.set("rb_layout_parts", "tiles")
    assert 'class="collection layout-auto"' in ready_client.get("/sets").text
    response = ready_client.get("/sets?layout=list")
    assert response.status_code == 303 and response.headers["location"] == "/sets"
    assert 'class="collection layout-list"' in ready_client.get("/sets").text


def test_set_list_follows_article_stock(ready_client):
    _, article_id = set_with(ready_client, "Starter Set", stock=0)
    page = ready_client.get("/sets").text
    for name in ("summary", "filters", "list"):
        assert f'data-live-region="{name}" data-live-topics="set article"' in page, name
    with sm_of(ready_client).begin() as s:
        stock.count_article(s, article_id, "stock", 4, "user")
    piece = ready_client.get("/sets", headers={regions.HEADER: "list"}).text
    assert '<span class="badge tone-ok">deliverable</span>' in piece


def test_empty_sets_list(ready_client):
    assert "No sets yet." in list_of(ready_client.get("/sets").text)


def test_set_edit_watches_its_set(ready_client):
    set_id, _ = set_with(ready_client, "Starter Set", stock=1)
    page = ready_client.get(f"/sets/{set_id}/edit").text
    assert f'data-live-region="form" data-live-topics="set:{set_id} article"' in page
    form = region_of(page, "form")
    assert re.search(rf'<form method="post" action="/sets/{set_id}/edit" data-live-watch="set:{set_id}">\s*<p class="live-stale" hidden', form)
    assert 'class="page-head"' in page


def test_new_set_form_offers_an_article_created_elsewhere(ready_client):
    page = ready_client.get("/sets/new").text
    assert 'data-live-region="form" data-live-topics="article"' in page
    assert "data-live-watch" not in page and 'class="page-head"' in page
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Template Z")
    piece = ready_client.get("/sets/new", headers={regions.HEADER: "form"}).text
    assert piece.startswith('<div class="live-region" data-live-region="form"')
    assert ">Template Z</option>" in piece


def test_more_rows_page_has_no_region_source(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
    data = {"csrf_token": csrf_of(ready_client, "/sets/new"), "form_token": "x", "name": "Typed Set",
            "rows": "5", "pos_0_qty": "1", "pos_0_articles": [str(a)], "action": "more"}
    response = ready_client.post("/sets/new", data=data)
    assert response.status_code == 200
    assert 'data-live-src=""' in response.text and 'value="Typed Set"' in response.text
