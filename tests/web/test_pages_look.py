"""The remaining pages in the new look: page head, booking forms as live regions (plan task 15)."""

import re

import pytest

from conftest import csrf_of, form_token_of, make_client, sm_of
from factories import make_article, make_part
from reprintbuddy.web import regions
from reprintbuddy.web.app import TEMPLATES_DIR

BOOKING_FORMS = ["inventory", "part-inventory", "part-receipt", "sale", "stock-in"]


def head_icon(html):
    """The symbol in the page head, or None."""
    found = re.search(r'<header class="page-head"><span class="page-icon">.*?#([a-z0-9-]+)"', html, re.S)
    return found.group(1) if found else None


def assert_one_head(html, where):
    assert html.count('class="page-head"') == 1, where
    assert len(re.findall(r"<h1[\s>]", html)) == 1, where


@pytest.mark.parametrize(
    ("path", "icon"),
    [
        ("/", "layout-dashboard"),
        ("/bookings", "arrow-left-right"),
        ("/journal", "history"),
        ("/settings", "settings"),
        ("/nosuch", "circle-alert"),
    ],
)
def test_my_pages_have_one_page_head_with_their_icon(ready_client, path, icon):
    page = ready_client.get(path)
    assert_one_head(page.text, path)
    assert head_icon(page.text) == icon, path



@pytest.fixture
def ids(ready_client):
    """One of each thing that has a page of its own."""
    from factories import make_order, make_set

    with sm_of(ready_client).begin() as s:
        article_id = make_article(s, name="Template A").id
        return {
            "article": article_id,
            "part": make_part(s, name="Insert M4").id,
            "set": make_set(s, "Set A", [(1, [article_id])]).id,
            "order": make_order(s, reference="Order 1001").id,
        }


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/bookings",
        "/journal",
        "/settings",
        "/nosuch",
        "/articles",
        "/articles/{article}",
        "/articles/{article}/edit",
        "/sets",
        "/sets/new",
        "/sets/{set}/edit",
        "/parts",
        "/parts/new",
        "/parts/{part}/edit",
        "/orders",
        "/orders/new",
        "/orders/{order}",
    ],
)
def test_every_app_page_has_one_page_head(ready_client, ids, path):
    page = ready_client.get(path.format(**ids))
    assert page.status_code == (404 if path == "/nosuch" else 200), path
    assert_one_head(page.text, path)


@pytest.mark.parametrize(
    "name",
    [p.name for p in sorted(TEMPLATES_DIR.rglob("*.html"))],
)
def test_no_template_uses_the_old_badges(name):
    text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    assert "badge ok" not in text and "badge wait" not in text, name


def test_restart_page_has_one_page_head(ops_env):
    page = ops_env.client.post("/settings/restart", data={"csrf_token": csrf_of(ops_env.client, "/settings")})
    assert page.status_code == 200
    assert_one_head(page.text, "restart")
    assert head_icon(page.text) == "refresh-cw"


def test_error_page_without_the_menu_has_the_head_too(admin_client):
    admin_client.cookies.clear()
    page = admin_client.post("/settings/language", data={})  # signed out: the page without the menu
    assert page.status_code == 403
    assert "sidebar-nav" not in page.text
    assert_one_head(page.text, "bare error")
    assert head_icon(page.text) == "circle-alert"


def test_booking_forms_are_regions(ready_client):
    page = ready_client.get("/bookings").text
    for name in BOOKING_FORMS:
        region = re.search(rf'<div class="live-region" data-live-region="{name}" data-live-topics="([^"]*)">', page)
        assert region, name
        assert region.group(1) == "article part", name
    assert "data-live-watch" not in page  # Design 4.6: general forms show no notice


def test_booking_page_follows_the_journal(ready_client):
    page = ready_client.get("/bookings").text
    assert '<div class="live-region" data-live-region="journal" data-live-topics="journal">' in page


def test_booking_region_brings_an_article_added_elsewhere(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Template Z")
        make_part(s, name="Insert Z")
    fetched = ready_client.get("/bookings", headers={regions.HEADER: "inventory part-receipt"})
    assert fetched.headers["x-live-regions"] == "inventory part-receipt"
    inventory, receipt = fetched.text.split('data-live-region="part-receipt"')
    assert "Template Z" in inventory
    assert "Insert Z" in receipt


@pytest.mark.parametrize(
    ("form", "fields"),
    [
        ("inventory", {"field": "stock", "counted": "lots"}),
        ("part-inventory", {"counted": "lots"}),
        ("part-receipt", {"qty": "lots"}),
        ("sale", {"qty": "lots"}),
        ("stock-in", {"qty": "lots"}),  # "deduct parts" left unticked
    ],
)
def test_failed_booking_page_has_no_region_source(ready_client, form, fields):  # Review Focus 2
    with sm_of(ready_client).begin() as s:
        thing = {"article_id": make_article(s, name="Template A").id, "part_id": make_part(s, name="Insert M4").id}
    data = {
        "csrf_token": csrf_of(ready_client, "/bookings"),
        "form_token": form_token_of(ready_client, "/bookings", f"/bookings/{form}"),
        ("part_id" if form.startswith("part") else "article_id"): str(
            thing["part_id" if form.startswith("part") else "article_id"]),
        "note": "typed by hand",
        **fields,
    }
    page = ready_client.post(f"/bookings/{form}", data=data)
    assert page.status_code == 200
    assert 'data-live-src=""' in page.text
    card = regions.extract(page.text, [form])[form]  # the typed values and the error stay in their own card
    assert 'value="typed by hand"' in card and 'value="lots"' in card
    assert 'role="alert"' in card
    others = page.text.replace(card, "")
    assert "typed by hand" not in others and 'role="alert"' not in others
    if form == "stock-in":
        assert re.search(r'<input name="consume_parts" type="checkbox"\s*>', card)


def test_bare_pages_keep_logo_and_no_menu(tmp_path, ready_client):
    ready_client.cookies.clear()
    pages = {
        "login": ready_client.get("/login"),
        "setup": make_client(tmp_path / "fresh.db").get("/setup"),
    }
    for name, page in pages.items():
        assert page.status_code == 200, name
        assert "sidebar-nav" not in page.text, name
        assert "brand-mark-large" in page.text, name
        assert "page-head" not in page.text, name  # the logo is the head of these pages


@pytest.mark.parametrize(
    "path",
    [
        "/", "/articles", "/articles/{article}", "/articles/{article}/edit", "/sets", "/parts",
        "/orders", "/orders/{order}", "/orders/new", "/bookings", "/journal", "/settings", "/nosuch",
    ],
)
def test_every_page_with_the_menu_has_a_subtitle(ready_client, ids, path):
    page = ready_client.get(path.format(**ids)).text
    assert "sidebar-nav" in page, path
    assert page.count('class="page-sub"') == 1, path


def test_new_subtitles_use_their_keys(ready_client, ids):
    from reprintbuddy.web.i18n import translate

    for path, key in [("/", "overview.subtitle"), ("/bookings", "bookings.subtitle"),
                      ("/journal", "journal.subtitle"), ("/articles/{article}/edit", "articles.edit_subtitle")]:
        page = ready_client.get(path.format(**ids)).text
        assert f'<p class="page-sub">{translate("en", key)}</p>' in page, path


def test_error_page_message_is_the_subtitle(ready_client):
    page = ready_client.get("/nosuch").text
    assert re.search(r'<p class="page-sub">[^<]+</p>', page)
    assert 'class="summary"' not in page


def test_archived_badge_of_part_and_set_forms_sits_in_the_subtitle(ready_client):
    from factories import make_set
    from reprintbuddy.models import utcnow

    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
        part = make_part(s, name="Insert M4", archived_at=utcnow()).id
        pset = make_set(s, "Set A", [(1, [a])])
        pset.archived_at = utcnow()
        set_id = pset.id
    for path in (f"/parts/{part}/edit", f"/sets/{set_id}/edit"):
        page = ready_client.get(path).text
        assert re.search(r'<p class="page-sub">[^<]*<span class="badge[^>]*>[^<]*</span></p>', page), path
        assert 'class="page-actions"' not in page, path


def test_no_template_uses_the_old_summary():
    for p in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = p.read_text(encoding="utf-8")
        assert "ui.summary" not in text and 'class="summary"' not in text, p.name


def test_article_detail_head_has_edit_as_main_button(ready_client, ids):
    page = ready_client.get(f"/articles/{ids['article']}").text
    head = page.split("</header>")[0]
    assert re.search(r'<div class="page-actions"><a class="btn" href="/articles/\d+/edit">.*?#pencil', head, re.S)
    assert "article:" in page and 'data-live-region="head"' in page
