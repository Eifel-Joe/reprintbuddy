"""The article pages in the new look: page head with the short state, live regions on the detail
and the edit page, the create flow with a page head (Spec 10.1, 10.5, plan Task 11)."""

import re

import pytest
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article

from reprintbuddy.domain import catalog, stock
from reprintbuddy.domain.catalog import ArticleInput
from reprintbuddy.models import utcnow
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def add(client, **fields):
    with sm_of(client).begin() as s:
        return make_article(s, **fields).id


def head_of(page):
    return re.search(r'<header class="page-head">.*?</header>', page, re.S).group(0)


def topics_of(page):
    """Region name -> its topics, in the order of the page."""
    return dict(re.findall(r'data-live-region="([^"]+)" data-live-topics="([^"]*)"', page))


def piece(client, path, name):
    """One region of a page, fetched the way live.js fetches it."""
    response = client.get(path, headers={regions.HEADER: name})
    assert response.status_code == 200
    assert response.headers["x-live-regions"] == name
    return response.text


def field_value(html, name):
    tag = re.search(rf'<input[^>]*\bname="{name}"[^>]*>', html).group(0)
    return re.search(r'\bvalue="([^"]*)"', tag).group(1)


def test_detail_head_shows_number_and_status(ready_client):
    a = add(ready_client, name="Template A", sku="A-100", stock=1, min_stock=4)
    head = head_of(ready_client.get(f"/articles/{a}").text)
    assert "#package" in head and "<h1>Template A</h1>" in head
    assert "A-100" in head
    assert '<span class="badge tone-bad">below minimum</span>' in head
    assert "paused" not in head and en("catalog.archived_badge") not in head


def test_detail_head_shows_paused_and_archived(ready_client):
    a = add(ready_client, sku="A-100", stock=4, min_stock=4, paused=True, archived_at=utcnow())
    head = head_of(ready_client.get(f"/articles/{a}").text)
    assert '<span class="badge tone-ok">enough</span>' in head
    assert '<span class="badge tone-neutral">paused</span>' in head
    assert f'<span class="badge tone-neutral">{en("catalog.archived_badge")}</span>' in head


def test_detail_buttons(ready_client):
    a = add(ready_client)
    page = ready_client.get(f"/articles/{a}").text
    assert re.search(rf'<a class="btn" href="/articles/{a}/edit">.*?{en("articles.edit")}</a>', page)
    assert re.search(rf'<a class="btn secondary" href="/journal\?article={a}">.*?#history.*?{en("journal.title")}</a>', page)
    assert re.search(rf'<a class="btn secondary" href="/articles">.*?#package.*?{en("articles.title")}</a>', page)
    assert "badge ok" not in page and "badge wait" not in page  # the old badges are gone here


def test_detail_regions_and_topics(ready_client):
    a = add(ready_client)
    page = ready_client.get(f"/articles/{a}").text
    topics = topics_of(page)
    assert topics["main"] == f"article:{a} part filament"  # the sort column shows filament labels
    assert topics["journal"] == "journal"
    main = regions.extract(page, ["main"])["main"]
    for label in ("articles.col.stock", "articles.col.reserved", "articles.col.available",
                  "articles.col.provisional", "articles.col.awaiting_assembly", "articles.target_quantity",
                  "articles.settings", "articles.filament_title", "articles.parts_title", "catalog.archive"):
        assert en(label) in main, label


def test_detail_head_follows_the_article(ready_client):
    # The state in the head changes with every booking, like the numbers below it.
    a = add(ready_client, sku="A-100", stock=4, min_stock=4)
    assert topics_of(ready_client.get(f"/articles/{a}").text)["head"] == f"article:{a}"
    with sm_of(ready_client).begin() as s:
        stock.count_article(s, a, "stock", 1, "user")
    head = piece(ready_client, f"/articles/{a}", "head")
    assert '<span class="badge tone-bad">below minimum</span>' in head


def test_detail_region_shows_a_booking_made_elsewhere(ready_client):
    a = add(ready_client, stock=3)
    with sm_of(ready_client).begin() as s:
        stock.count_article(s, a, "stock", 9, "user")
    main = piece(ready_client, f"/articles/{a}", "main")
    assert re.search(rf'{en("articles.col.stock")}</span>\s*<span class="stat-value">9<', main)
    assert "<html" not in main


def test_detail_target_has_a_bar(ready_client):
    a = add(ready_client, stock=3, min_stock=4, buffer=2)
    main = regions.extract(ready_client.get(f"/articles/{a}").text, ["main"])["main"]
    assert re.search(
        rf'{en("articles.target_quantity")}</span>\s*<span class="stat-value">6</span>'
        r'<span class="bar tone-bad" role="progressbar"[^>]*aria-valuenow="50"',
        main,
    )


def test_detail_error_stays_outside_the_regions(ready_client):
    a = add(ready_client, provisional=1)  # provisional units: archiving is refused
    path = f"/articles/{a}"
    data = {
        "csrf_token": csrf_of(ready_client, path),
        "form_token": form_token_of(ready_client, path, f"{path}/archive"),
    }
    response = ready_client.post(f"{path}/archive", data=data)
    assert response.status_code != 303
    message = en("articles.archive_provisional")
    assert message in response.text
    inside = regions.extract(response.text, ["head", "main", "journal"])
    assert inside and all(message not in html for html in inside.values())
    assert 'data-live-src=""' in response.text


def test_edit_form_watches_the_settings_of_its_article(ready_client):
    a = add(ready_client)
    page = ready_client.get(f"/articles/{a}/edit").text
    form = re.search(rf'<form[^>]*data-live-watch="article-settings:{a}"[^>]*>(.*?)</form>', page, re.S)
    assert form, "no watched form"
    assert '<p class="live-stale" hidden' in form.group(1)
    assert en("live.changed") in form.group(1)
    assert topics_of(page) == {"form": f"article-settings:{a} part"}


def test_edit_head_has_the_name_without_a_short_state(ready_client):
    a = add(ready_client, name="Template A", sku="A-100")
    head = head_of(ready_client.get(f"/articles/{a}/edit").text)
    assert "#package" in head and "<h1>Template A</h1>" in head
    assert "summary" not in head and "badge" not in head


def test_edit_region_brings_the_saved_values(ready_client):
    a = add(ready_client, min_stock=1)
    with sm_of(ready_client).begin() as s:
        catalog.update_article(s, a, ArticleInput(name="Template A", min_stock=7))
    form = piece(ready_client, f"/articles/{a}/edit", "form")
    assert field_value(form, "min_stock") == "7"


def test_failed_edit_keeps_typed_values_without_region_source(ready_client):  # Review Focus 2
    a = add(ready_client, min_stock=1)
    data = {
        "csrf_token": csrf_of(ready_client, f"/articles/{a}/edit"),
        "name": "Template A", "sku": "", "units_per_print": "1", "min_stock": "-1", "buffer": "3",
        "target": "",
    }
    response = ready_client.post(f"/articles/{a}/edit", data=data)
    assert response.status_code == 200
    assert field_value(response.text, "min_stock") == "-1"
    assert field_value(response.text, "buffer") == "3"
    assert 'data-live-src=""' in response.text


@pytest.mark.parametrize("path", ["/articles/new", "/articles/new/file/1", "/articles/new/file/1/plate/1"])
def test_create_flow_pages_have_the_head(bb_client, path):
    page = bb_client.get(path)
    assert page.status_code == 200
    assert 'class="page-head"' in page.text and "#package" in head_of(page.text)
    assert "data-live-region" not in page.text
    key = "articles.choose_plate" if path.endswith("/file/1") else "articles.new_subtitle"
    assert f'<p class="page-sub">{en(key)}</p>' in head_of(page.text)


def test_create_flow_subtitles_in_german(bb_client):
    token = csrf_of(bb_client, "/settings")
    bb_client.post("/settings/language", data={"csrf_token": token, "language": "de"})
    for path, key in [("/articles/new", "articles.new_subtitle"), ("/articles/new/file/1", "articles.choose_plate"),
                      ("/articles/new/file/1/plate/1", "articles.new_subtitle")]:
        head = head_of(bb_client.get(path).text)
        assert f'<p class="page-sub">{translate("de", key)}</p>' in head, path
