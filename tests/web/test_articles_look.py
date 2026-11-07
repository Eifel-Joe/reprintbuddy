"""The article list in the new look: status badges, short summary, search, filters, tiles or list
and live regions (Spec 10.1, plan Task 10)."""

import re

import pytest
from conftest import ADMIN, PASSWORD, csrf_of, sm_of
from factories import make_article
from fastapi.testclient import TestClient

from reprintbuddy import settings as st
from datetime import date

from reprintbuddy.domain import orders
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.models import Article, utcnow
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


def kpis_of(page):
    """The cards of the summary region as (value, label) pairs."""
    block = regions.extract(page, ["summary"])["summary"]
    return [
        (int(value), label)
        for value, label in re.findall(r'<span class="kpi-value">(\d+)</span><span class="kpi-label">(.*?)</span>', block)
    ]


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
    assert kpis_of(page) == [(1, "Articles")]  # paused is a badge and a filter, not a card


def test_kpi_cards_count_present_states(ready_client):
    four_states(ready_client)
    page = ready_client.get("/articles").text
    assert kpis_of(page) == [(4, "Articles"), (1, "overbooked"), (1, "below minimum"), (1, "low")]
    assert 'class="kpis"' in regions.extract(page, ["summary"])["summary"]
    assert '<p class="summary">' not in page


def test_kpi_cards_in_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    four_states(ready_client)
    assert kpis_of(ready_client.get("/articles").text) == [
        (4, "Artikel"), (1, "überbucht"), (1, "unter Minimum"), (1, "knapp")
    ]


def test_kpi_cards_skip_states_without_entries(ready_client):
    add(ready_client, name="Template D", stock=4, min_stock=4)
    assert kpis_of(ready_client.get("/articles").text) == [(1, "Articles")]


def test_archive_has_one_card(ready_client):
    four_states(ready_client)
    add(ready_client, name="Template E", archived_at=utcnow())
    assert kpis_of(ready_client.get("/articles?view=archived").text) == [(1, "Archived articles")]


def test_head_has_subtitle_and_new_button(ready_client):
    page = ready_client.get("/articles").text
    head = page[page.index('<header class="page-head">') : page.index("</header>")]
    assert f'<p class="page-sub">{en("articles.subtitle")}</p>' in head
    actions = head[head.index('<div class="page-actions">') :]
    assert 'href="/articles/new"' in actions and en("articles.new") in actions
    assert 'href="/articles/new"' not in page[page.index('<div class="toolbar">') : page.index("<!--live:list-->")]


def row_actions_of(page):
    block = list_of(page)
    return block[block.index('<td class="actions-col">') :].split("</td>")[0]


def test_row_has_edit_journal_and_archive(ready_client):
    a = add(ready_client, name="Template A")
    page = ready_client.get("/articles").text
    cell = row_actions_of(page)
    assert f'href="/articles/{a}/edit"' in cell and f'title="{en("articles.edit")}"' in cell
    assert f'href="/journal?article={a}"' in cell and f'title="{en("journal.title")}"' in cell
    assert f'<form class="icon-form" method="post" action="/articles/{a}/archive">' in cell
    assert 'name="csrf_token"' in cell and 'name="form_token"' in cell
    assert f'title="{en("catalog.archive")}"' in cell and "/restore" not in cell
    assert f'<span class="sr-only">{en("list.col.actions")}</span>' in list_of(page)
    assert list_of(page).count(f"/articles/{a}/archive") == 1  # tiles have no actions


def test_archived_row_has_journal_and_restore(ready_client):
    a = add(ready_client, name="Template A", archived_at=utcnow())
    cell = row_actions_of(ready_client.get("/articles?view=archived").text)
    assert f'href="/journal?article={a}"' in cell and f'action="/articles/{a}/restore"' in cell
    assert "/edit" not in cell and "/archive" not in cell.replace("archive-restore", "")


def test_archive_from_the_row_goes_to_the_detail_page(ready_client):
    a = add(ready_client, name="Template A")
    page = ready_client.get("/articles").text
    response = ready_client.post(
        f"/articles/{a}/archive",
        data={"csrf_token": csrf_of(ready_client, "/articles"), "form_token": row_token(page, a)},
    )
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{a}"
    with sm_of(ready_client).begin() as s:
        assert s.get(Article, a).archived_at is not None


def test_archive_from_the_row_is_refused_with_an_open_order(ready_client):
    a = add(ready_client, name="Template A", stock=2)
    with sm_of(ready_client).begin() as s:
        orders.create_order(s, reference="Order 1001", order_date=date(2026, 1, 15), items=[ArticleItem(a, 1)], actor="user")
    page = ready_client.get("/articles").text
    response = ready_client.post(
        f"/articles/{a}/archive",
        data={"csrf_token": csrf_of(ready_client, "/articles"), "form_token": row_token(page, a)},
    )
    assert response.status_code != 303 and response.status_code == 200  # the status the route uses for a refusal
    assert en("articles.archive_open_orders") in response.text
    assert 'data-live-region="list"' not in response.text and "Template A" in response.text  # the detail page, not the list
    with sm_of(ready_client).begin() as s:
        assert s.get(Article, a).archived_at is None


def row_token(page, article_id):
    form = re.search(rf'<form class="icon-form"[^>]*action="/articles/{article_id}/archive">(.*?)</form>', page, re.S).group(1)
    return re.search(r'name="form_token" value="([^"]+)"', form).group(1)


def test_summary_region_carries_cards_and_fresh_form_tokens(ready_client):
    a = add(ready_client, name="Template A", stock=0, reserved=1)
    response = ready_client.get("/articles", headers={regions.HEADER: "summary list"})
    assert 'data-live-region="summary"' in response.text and 'class="kpis"' in response.text
    token = row_token(response.text, a)
    again = ready_client.post(
        f"/articles/{a}/archive",
        data={"csrf_token": csrf_of(ready_client, "/articles"), "form_token": token},
    )
    assert again.status_code == 303  # the token of the refetched region is valid


def test_filter_shows_only_that_state_and_the_counts_stay(ready_client):
    four_states(ready_client)
    page = ready_client.get("/articles?status=low").text
    listed = list_of(page)
    assert "Template B" in listed
    for other in ("Template A", "Template C", "Template D"):
        assert other not in listed, other
    assert '<a class="filter active" href="/articles?status=low" aria-current="true">' in page
    assert 'All <span class="count">4</span>' in page
    assert kpis_of(page)[0] == (4, "Articles")  # the cards count all articles


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


def test_negative_available_is_red_in_tiles_too(ready_client):  # Spec 6.4
    # `.tile-stats strong` sets the text colour; `.neg` alone would lose against it.
    css = ready_client.get("/static/css/reprintbuddy.css").text
    assert ".tile-stats strong.neg { color: var(--danger); }" in css


def test_search_narrows_the_filter_counts_not_the_summary(ready_client):
    four_states(ready_client)
    page = ready_client.get("/articles?q=Template%20B").text
    filters = regions.extract(page, ["filters"])["filters"]
    assert 'All <span class="count">1</span>' in filters
    assert re.findall(r'href="([^"]*)"', filters) == ["/articles?q=Template+B", "/articles?q=Template+B&amp;status=low"]
    assert "Template B" in list_of(page) and "Template A" not in list_of(page)
    assert kpis_of(page)[0] == (4, "Articles")


def test_name_and_number_are_escaped_in_list_and_tile(ready_client):
    article = add(ready_client, name="Template <A>", sku="A<100")
    listed = list_of(ready_client.get("/articles").text)
    assert f'<a href="/articles/{article}">Template &lt;A&gt;</a><br><span class="muted">A&lt;100</span>' in listed
    assert '<div>Template &lt;A&gt;<br><span class="muted">A&lt;100</span></div>' in listed
    assert "<A>" not in listed and "A<100" not in listed


# ---- archive and restore from the list go back to the list ------------------------------------

def row_form(page, article_id, action):
    """The form of a row action: (hidden fields by name, form_token)."""
    form = re.search(
        rf'<form class="icon-form"[^>]*action="/articles/{article_id}/{action}">(.*?)</form>', page, re.S
    ).group(1)
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form))


def post_row(client, page, article_id, action, **extra):
    fields = row_form(page, article_id, action)
    data = {"csrf_token": csrf_of(client, "/articles"), "form_token": fields["form_token"], **extra}
    if "next" in fields:
        data.setdefault("next", fields["next"])
    return client.post(f"/articles/{article_id}/{action}", data=data)


def test_row_forms_send_the_list_they_belong_to(ready_client):
    a = add(ready_client, name="Template A")
    b = add(ready_client, name="Template B", archived_at=utcnow())
    assert row_form(ready_client.get("/articles").text, a, "archive")["next"] == "/articles"
    assert row_form(ready_client.get("/articles?view=archived").text, b, "restore")["next"] == "/articles"


def test_archive_from_the_row_goes_back_to_the_list_with_the_message(ready_client):
    a = add(ready_client, name="Template A")
    add(ready_client, name="Template B")
    page = ready_client.get("/articles").text
    response = post_row(ready_client, page, a, "archive")
    assert response.status_code == 303 and response.headers["location"] == "/articles"
    with sm_of(ready_client).begin() as s:
        assert s.get(Article, a).archived_at is not None
    listed = ready_client.get("/articles").text
    assert en("articles.flash_archived", name="Template A") in listed
    assert "Template B" in list_of(listed) and "Template A" not in list_of(listed)


def test_restore_from_the_archive_row_goes_to_the_active_list_with_the_message(ready_client):
    a = add(ready_client, name="Template A", archived_at=utcnow())
    page = ready_client.get("/articles?view=archived").text
    response = post_row(ready_client, page, a, "restore")
    assert response.status_code == 303 and response.headers["location"] == "/articles"
    with sm_of(ready_client).begin() as s:
        assert s.get(Article, a).archived_at is None
    listed = ready_client.get("/articles").text
    assert en("articles.flash_restored", name="Template A") in listed and "Template A" in list_of(listed)


@pytest.mark.parametrize("foreign", ["//evil.example", "https://evil.example/x", "/\\evil.example", ""])
def test_a_foreign_next_falls_back_to_the_detail_page(ready_client, foreign):
    a = add(ready_client, name="Template A")
    page = ready_client.get("/articles").text
    response = post_row(ready_client, page, a, "archive", next=foreign)
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{a}"


def test_a_used_form_token_from_the_list_goes_back_to_the_list(ready_client):
    a = add(ready_client, name="Template A")
    page = ready_client.get("/articles").text
    post_row(ready_client, page, a, "archive")
    again = post_row(ready_client, page, a, "archive")  # the same token a second time
    assert again.status_code == 303 and again.headers["location"] == "/articles"


def test_archive_refused_from_the_list_still_shows_the_detail_page_with_the_reason(ready_client):
    a = add(ready_client, name="Template A", stock=2)
    with sm_of(ready_client).begin() as s:
        orders.create_order(s, reference="Order 1001", order_date=date(2026, 1, 15), items=[ArticleItem(a, 1)], actor="user")
    response = post_row(ready_client, ready_client.get("/articles").text, a, "archive")
    assert response.status_code == 200 and en("articles.archive_open_orders") in response.text
    with sm_of(ready_client).begin() as s:
        assert s.get(Article, a).archived_at is None


def test_the_detail_page_forms_stay_on_the_detail_page(ready_client):
    a = add(ready_client, name="Template A")
    detail = ready_client.get(f"/articles/{a}").text
    assert f'action="/articles/{a}/archive"' in detail and 'name="next"' not in detail
    response = ready_client.post(
        f"/articles/{a}/archive",
        data={"csrf_token": csrf_of(ready_client, f"/articles/{a}"), "form_token": re.search(r'name="form_token" value="([^"]+)"', detail).group(1)},
    )
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{a}"
