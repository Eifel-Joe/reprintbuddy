"""The parts pages in the new look: status badges, short summary, search, filters, tiles or list
and live regions (Spec 10.1, plan Task 12)."""

import re

import pytest
from conftest import csrf_of, sm_of
from factories import make_part

from reprintbuddy import settings as st
from reprintbuddy.domain import stock
from reprintbuddy.models import Part, utcnow
from reprintbuddy.web import regions


def add(client, **fields):
    with sm_of(client).begin() as s:
        return make_part(s, **fields).id


def two_states(client):
    """One part below its minimum, one with enough."""
    add(client, name="Insert M4", stock=5, min_stock=5, pack_size=50, supplier="Example Supplier")
    add(client, name="Magnet 10x2", stock=2, min_stock=5)


def summary_of(page):
    return re.search(r'<p class="summary">(.*?)</p>', page, re.S).group(1)


def text_of(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def region_of(page, name):
    return regions.extract(page, [name])[name]


def list_of(page):
    return region_of(page, "list")


@pytest.mark.parametrize(
    "fields,text,classes",
    [
        (dict(stock=5, min_stock=5), "enough", "tone-ok"),
        (dict(stock=2, min_stock=5), "below minimum", "tone-bad"),
    ],
)
def test_part_badges(ready_client, fields, text, classes):
    add(ready_client, **fields)
    assert f'<span class="badge {classes}">{text}</span>' in list_of(ready_client.get("/parts").text)


def test_parts_summary_and_filter(ready_client):
    two_states(ready_client)
    page = ready_client.get("/parts").text
    assert text_of(summary_of(page)) == "2 parts · 1 below minimum"
    assert summary_of(page).count('class="dot tone-bad"') == 1
    only = ready_client.get("/parts?status=below_min").text
    assert "Magnet 10x2" in list_of(only) and "Insert M4" not in list_of(only)
    filters = region_of(only, "filters")
    assert re.search(r'class="filter active" href="/parts\?status=below_min"', filters)
    assert re.search(r'>All <span class="count">2</span>', filters)


def test_parts_summary_in_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    two_states(ready_client)
    assert text_of(summary_of(ready_client.get("/parts").text)) == "2 Zukaufteile · 1 unter Minimum"


def test_unknown_part_filter_is_ignored(ready_client):
    two_states(ready_client)
    page = ready_client.get("/parts?status=low").text  # a state of articles, not of parts
    assert "Magnet 10x2" in list_of(page) and "Insert M4" in list_of(page)


def test_parts_search_by_name(ready_client):
    two_states(ready_client)
    page = ready_client.get("/parts?q=MAGNET").text
    assert "Magnet 10x2" in list_of(page) and "Insert M4" not in list_of(page)
    assert 'placeholder="Name"' in page
    nothing = ready_client.get("/parts?q=%25").text
    assert "Nothing matches the search or filter." in list_of(nothing)


def test_parts_search_keeps_filter_and_view(ready_client):
    two_states(ready_client)
    page = ready_client.get("/parts?status=below_min").text
    form = re.search(r'<form class="search".*?</form>', page, re.S).group(0)
    assert '<input type="hidden" name="status" value="below_min">' in form
    archive = ready_client.get("/parts?view=archived").text
    form = re.search(r'<form class="search".*?</form>', archive, re.S).group(0)
    assert '<input type="hidden" name="view" value="archived">' in form


def test_parts_list_and_tiles(ready_client):
    two_states(ready_client)
    part_id = add(ready_client, name="Spring 5mm", stock=3, min_stock=6, unit="box")
    listing = list_of(ready_client.get("/parts").text)
    assert 'class="collection layout-auto"' in listing
    table = re.search(r"<table>.*?</table>", listing, re.S).group(0)
    assert f'<a href="/parts/{part_id}/edit">Spring 5mm</a>' in table
    assert "3 box" in table and f'href="/journal?part={part_id}"' in table
    assert "Example Supplier" in table
    tiles = re.search(r'<div class="tiles">.*', listing, re.S).group(0)
    tile = re.search(rf'<a class="tile" href="/parts/{part_id}/edit">.*?</a>', tiles, re.S).group(0)
    assert "Spring 5mm" in tile and '<span class="badge tone-bad">below minimum</span>' in tile
    assert 'aria-valuenow="50"' in tile  # 3 of 6
    supplier_tile = re.search(r'<a class="tile"[^>]*>(?:(?!</a>).)*Insert M4.*?</a>', tiles, re.S).group(0)
    assert 'class="tile-foot"' in supplier_tile and "Example Supplier" in supplier_tile
    assert 'class="tile-foot"' not in tile  # no supplier, no footer


def test_parts_head_and_buttons(ready_client):
    page = ready_client.get("/parts").text
    assert 'class="page-head"' in page and "#puzzle" in page
    assert re.search(r'<a class="btn" href="/parts/new">.*?#plus.*?New part</a>', page, re.S)
    assert re.search(r'<a class="btn secondary" href="/parts\?view=archived">.*?#archive', page, re.S)
    archive = ready_client.get("/parts?view=archived").text
    assert re.search(r'<a class="btn secondary" href="/parts">.*?#archive.*?Show active', archive, re.S)


def test_archived_parts_show_the_archived_badge_and_no_filters(ready_client):
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Old Clip", stock=0, min_stock=5, archived_at=utcnow())
    page = ready_client.get("/parts?view=archived").text
    assert '<span class="badge tone-neutral">Archived</span>' in list_of(page)
    assert "below minimum" not in list_of(page)
    assert 'data-live-region="filters"' not in page
    assert text_of(summary_of(page)) == "1 part"


def test_parts_view_choice_is_its_own(ready_client):
    two_states(ready_client)
    ready_client.cookies.set("rb_layout_articles", "tiles")
    assert 'class="collection layout-auto"' in ready_client.get("/parts").text
    response = ready_client.get("/parts?status=below_min&layout=tiles")
    assert response.status_code == 303 and response.headers["location"] == "/parts?status=below_min"
    assert 'class="collection layout-tiles"' in ready_client.get("/parts").text


def test_parts_list_regions_and_topics(ready_client):
    two_states(ready_client)
    page = ready_client.get("/parts").text
    for name in ("summary", "filters", "list"):
        assert f'data-live-region="{name}" data-live-topics="part"' in page, name


def test_part_inventory_elsewhere_reaches_the_list_region(ready_client):  # Design 8
    part_id = add(ready_client, name="Insert M4", stock=2, min_stock=5)
    with sm_of(ready_client).begin() as s:
        stock.count_part(s, part_id, 12, "user")
    response = ready_client.get("/parts", headers={regions.HEADER: "list"})
    assert response.status_code == 200
    assert "12 pcs" in response.text and "enough" in response.text


def test_empty_parts_list(ready_client):
    page = ready_client.get("/parts").text
    assert "No purchased parts yet." in list_of(page)


def test_part_edit_watches_its_settings(ready_client):
    part_id = add(ready_client, name="Insert M4", stock=2)
    page = ready_client.get(f"/parts/{part_id}/edit").text
    form = region_of(page, "form")
    assert f'data-live-topics="part-settings:{part_id}"' in page
    assert re.search(rf'<form method="post" action="/parts/{part_id}/edit" data-live-watch="part-settings:{part_id}">\s*<p class="live-stale" hidden', form)
    assert 'class="page-head"' in page


def test_part_edit_region_brings_the_saved_values(ready_client):
    part_id = add(ready_client, name="Insert M4", min_stock=1)
    with sm_of(ready_client).begin() as s:
        s.get(Part, part_id).min_stock = 7
    piece = ready_client.get(f"/parts/{part_id}/edit", headers={regions.HEADER: "form"}).text
    assert piece.startswith('<div class="live-region" data-live-region="form"')
    assert re.search(r'name="min_stock"[^>]*value="7"', piece)


def test_failed_part_edit_has_no_region_source(ready_client):
    part_id = add(ready_client, name="Insert M4")
    data = {"csrf_token": csrf_of(ready_client, f"/parts/{part_id}/edit"), "name": "Insert M4", "unit": "pcs",
            "min_stock": "-1", "pack_size": "1", "min_order_packs": "1", "lead_time_days": "0",
            "safety_value": "0", "safety_unit": "days"}
    response = ready_client.post(f"/parts/{part_id}/edit", data=data)
    assert response.status_code == 200
    assert 'data-live-src=""' in response.text and 'value="-1"' in response.text


def test_new_part_form_has_no_region(ready_client):
    page = ready_client.get("/parts/new").text
    assert 'class="page-head"' in page
    assert "data-live-region" not in page and "data-live-watch" not in page
