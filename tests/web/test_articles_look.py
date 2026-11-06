"""The article list in the new look: status badges, short summary, search, filters, tiles or list
and live regions (Spec 10.1, plan Task 10)."""

import re

import pytest
from conftest import ADMIN, PASSWORD, csrf_of, sm_of
from factories import make_article
from fastapi.testclient import TestClient

from reprintbuddy import settings as st
from reprintbuddy.models import utcnow
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def add(client, **fields):
    with sm_of(client).begin() as s:
        return make_article(s, **fields).id


def four_states(client):
    """One article per state: overbooked, low, below minimum, enough."""
    add(client, name="Template A", sku="A-100", stock=0, reserved=1)
    add(client, name="Template B", sku="B-200", stock=1, min_stock=4, provisional=3)
    add(client, name="Template C", stock=1, min_stock=4)
    add(client, name="Template D", stock=4, min_stock=4)


def summary_of(page):
    return re.search(r'<p class="summary">(.*?)</p>', page, re.S).group(1)


def text_of(html):
    return re.sub(r"<[^>]+>", "", html).strip()


def list_of(page):
    return regions.extract(page, ["list"])["list"]


@pytest.mark.parametrize(
    "fields,text,classes",
    [
        (dict(stock=0, reserved=1), "overbooked", "tone-bad flag"),
        (dict(stock=1, min_stock=4, provisional=3), "low", "tone-warn"),
        (dict(stock=1, min_stock=4), "below minimum", "tone-bad"),
        (dict(stock=4, min_stock=4), "enough", "tone-ok"),
    ],
)
def test_status_badge_follows_the_rules(ready_client, fields, text, classes):
    add(ready_client, **fields)
    page = ready_client.get("/articles").text
    assert f'<span class="badge {classes}">{text}</span>' in list_of(page)


def test_paused_article_has_a_grey_badge(ready_client):
    add(ready_client, paused=True)
    page = ready_client.get("/articles").text
    assert '<span class="badge tone-neutral">paused</span>' in list_of(page)
    assert '<span class="badge tone-ok">enough</span>' in list_of(page)  # the state stays
    assert "1 paused" in text_of(summary_of(page))


def test_summary_counts_present_states(ready_client):
    four_states(ready_client)
    summary = summary_of(ready_client.get("/articles").text)
    text = text_of(summary)
    for part in ("4 articles", "1 overbooked", "1 below minimum", "1 low"):
        assert part in text, part
    assert "enough" not in text
    assert summary.count('class="dot ') == 3


def test_summary_in_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    four_states(ready_client)
    summary = summary_of(ready_client.get("/articles").text)
    assert text_of(summary) == "4 Artikel · 1 überbucht · 1 unter Minimum · 1 knapp"


def test_filter_shows_only_that_state_and_the_counts_stay(ready_client):
    four_states(ready_client)
    page = ready_client.get("/articles?status=low").text
    listed = list_of(page)
    assert "Template B" in listed
    for other in ("Template A", "Template C", "Template D"):
        assert other not in listed, other
    assert '<a class="filter active" href="/articles?status=low" aria-current="true">' in page
    assert 'All <span class="count">4</span>' in page
    assert "4 articles" in text_of(summary_of(page))  # the summary counts all articles


def test_unknown_filter_is_ignored(ready_client):
    four_states(ready_client)
    response = ready_client.get("/articles?status=bogus")
    assert response.status_code == 200
    listed = list_of(response.text)
    for name in ("Template A", "Template B", "Template C", "Template D"):
        assert name in listed, name


def test_search_by_name_or_number(ready_client):
    four_states(ready_client)
    page = ready_client.get("/articles?q=a-1").text
    listed = list_of(page)
    assert "A-100" in listed and "Template A" in listed
    assert "Template B" not in listed and "Template C" not in listed
    assert 'value="a-1"' in page
    by_name = list_of(ready_client.get("/articles?q=template%20c").text)
    assert "Template C" in by_name and "Template D" not in by_name
    nothing = ready_client.get("/articles?q=%25")
    assert nothing.status_code == 200
    assert en("list.no_match") in list_of(nothing.text)


def test_search_keeps_the_filter_and_the_archive_view(ready_client):
    four_states(ready_client)
    page = ready_client.get("/articles?status=low").text
    assert '<input type="hidden" name="status" value="low">' in page
    assert 'name="view"' not in page
    archived = ready_client.get("/articles?view=archived").text
    assert '<input type="hidden" name="view" value="archived">' in archived
    assert 'name="status"' not in archived


def test_both_views_without_a_choice(ready_client):
    add(ready_client)
    page = ready_client.get("/articles").text
    assert 'class="collection layout-auto"' in page
    assert "<table>" in page and 'class="tiles"' in page


def independent_login(app):
    """A second browser that signs in on its own."""
    other = TestClient(app, follow_redirects=False)
    token = csrf_of(other, "/login")
    response = other.post("/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD})
    assert response.status_code == 303
    return other


def test_view_choice_is_remembered_per_browser(ready_client):  # Design 8
    add(ready_client)
    response = ready_client.get("/articles?layout=tiles")
    assert response.status_code == 303 and response.headers["location"] == "/articles"
    assert 'class="collection layout-tiles"' in ready_client.get("/articles").text
    other = independent_login(ready_client.app)
    assert 'class="collection layout-auto"' in other.get("/articles").text


def test_list_regions_and_topics(ready_client):
    four_states(ready_client)
    page = ready_client.get("/articles").text
    for name in ("summary", "filters", "list"):
        assert f"<!--live:{name}-->" in page and f"<!--/live:{name}-->" in page, name
        assert f'data-live-region="{name}" data-live-topics="article"' in page, name
    response = ready_client.get("/articles", headers={regions.HEADER: "list summary"})
    assert response.status_code == 200
    assert response.headers["x-live-regions"] == "list summary"
    assert 'data-live-region="list"' in response.text and 'data-live-region="summary"' in response.text
    assert "<html" not in response.text and "Template A" in response.text


def test_thumbnail_or_placeholder(ready_client):
    with_picture = add(ready_client, name="Template A", thumbnail=b"x")
    page = ready_client.get("/articles").text
    assert f'<img class="thumb-sm" src="/articles/{with_picture}/thumbnail"' in page
    assert "thumb-placeholder" not in page
    add(ready_client, name="Template B")
    assert "thumb-placeholder" in ready_client.get("/articles").text


def test_tile_footer_names_what_is_running(ready_client):
    add(ready_client, name="Template A", provisional=2)
    add(ready_client, name="Template B", awaiting_assembly=1)
    add(ready_client, name="Template C")
    page = ready_client.get("/articles").text
    assert "2 to check" in page and "1 to assemble" in page
    assert page.count('class="tile-foot"') == 2  # nothing running, no footer


def test_usable_without_javascript(ready_client):
    add(ready_client)
    page = ready_client.get("/articles").text
    assert '<a href="/articles?layout=list"' in page
    assert '<a href="/articles?layout=tiles"' in page
    assert '<form class="search" method="get"' in page


def test_archive_view_shows_archived_badge_and_no_filters(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Template A", stock=0, reserved=1, archived_at=utcnow())
    page = ready_client.get("/articles?view=archived").text
    listed = list_of(page)
    assert "Template A" in listed
    assert f'<span class="badge tone-neutral">{en("catalog.archived_badge")}</span>' in listed
    assert "overbooked" not in listed
    assert "<!--live:filters-->" not in page and 'class="filters"' not in page


def button(href, cls, icon):
    """A link button that starts with the given symbol."""
    return re.compile(rf'<a class="{cls}" href="{re.escape(href)}"><svg[^>]*><use href="[^"]*#{icon}">')


def test_toolbar_buttons(ready_client):
    page = ready_client.get("/articles").text
    assert button("/articles/new", "btn", "plus").search(page)
    assert button("/journal", "btn secondary", "history").search(page)
    assert button("/articles?view=archived", "btn secondary", "archive").search(page)
    assert button("/articles", "btn secondary", "archive").search(ready_client.get("/articles?view=archived").text)
    head = page.split('class="page-head"')[1].split("</header>")[0]
    assert "#package" in head and "<h1>Articles</h1>" in head
