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
    ready_client.get("/bookings")
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Template Z")
        make_part(s, name="Insert Z")
    fetched = ready_client.get("/bookings", headers={regions.HEADER: "inventory part-receipt"})
    assert fetched.headers["x-live-regions"] == "inventory part-receipt"
    inventory, receipt = fetched.text.split('data-live-region="part-receipt"')
    assert "Template Z" in inventory
    assert "Insert Z" in receipt


def test_failed_booking_page_has_no_region_source(ready_client):  # Review Focus 2
    with sm_of(ready_client).begin() as s:
        article_id = make_article(s, name="Template A").id
    data = {
        "csrf_token": csrf_of(ready_client, "/bookings"),
        "form_token": form_token_of(ready_client, "/bookings", "/bookings/inventory"),
        "article_id": str(article_id),
        "field": "stock",
        "counted": "lots",
        "note": "typed by hand",
    }
    page = ready_client.post("/bookings/inventory", data=data)
    assert page.status_code == 200
    assert 'data-live-src=""' in page.text
    assert 'value="typed by hand"' in page.text


def test_my_templates_do_not_use_the_old_badges():
    for name in ["bookings.html", "journal.html", "overview.html", "settings.html", "settings_ops.html",
                 "error.html", "restarting.html", "_journal_table.html"]:
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert "badge ok" not in text and "badge wait" not in text, name


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
