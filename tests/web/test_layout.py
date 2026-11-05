import re
from pathlib import Path

import pytest
from conftest import csrf_of

from reprintbuddy import settings as st
from reprintbuddy.web import i18n

WEB = Path(i18n.__file__).parent

NAV_EN = [
    "Overview",
    "Articles",
    "Sets",
    "Orders",
    "Bookings",
    "To check",
    "Assembly",
    "Queue",
    "Filament",
    "Parts",
    "Reorder",
    "Settings",
]


def _nav_labels(html):
    sidebar = re.search(r'<nav class="sidebar-nav".*?</nav>', html, re.S).group(0)
    return re.findall(r'<a [^>]*class="nav-link[^"]*"[^>]*>\s*(.*?)\s*</a>', sidebar, re.S)


def test_overview_english(ready_client):
    response = ready_client.get("/")
    assert response.status_code == 200
    assert _nav_labels(response.text)[:12] == NAV_EN
    assert "Overview" in response.text
    assert "Nothing here yet" in response.text


def test_overview_german(ready_client):
    with ready_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    response = ready_client.get("/")
    assert "Übersicht" in response.text
    assert "Bestellvorschläge" in response.text
    assert '<html lang="de">' in response.text


def test_active_nav_item_is_marked(ready_client):
    html = ready_client.get("/").text
    active = re.findall(r'class="nav-link active"[^>]*>\s*(.*?)\s*</a>', html, re.S)
    assert active and set(active) == {"Overview"}


def test_css_served(ready_client):
    response = ready_client.get("/static/css/reprintbuddy.css")
    assert response.status_code == 200
    assert "#00ae42" in response.text
    for token in ("#0f172a", "#1e293b", "#334155", "#f1f5f9", "#94a3b8", "#64748b"):
        assert token in response.text


def test_css_is_reachable_without_login(client):
    assert client.get("/static/css/reprintbuddy.css").status_code == 200


def test_no_external_urls():
    pattern = re.compile(r"https?://")
    offenders = [
        str(path.relative_to(WEB))
        for folder in ("templates", "static")
        for path in (WEB / folder).rglob("*")
        if path.is_file() and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_bambuddy_link(ready_client):
    html = ready_client.get("/").text
    match = re.search(r'<a [^>]*href="(http://bambuddy\.test:8000)"[^>]*>', html)
    assert match, "link to the stored Bambuddy address missing"
    assert 'target="_blank"' in match.group(0)
    assert "noopener" in match.group(0)


def test_logout_form_posts_with_csrf(ready_client):
    html = ready_client.get("/").text
    assert re.search(r'<form method="post" action="/logout"', html)
    token = csrf_of(ready_client, "/")
    assert ready_client.post("/logout", data={"csrf_token": token}).status_code == 303


def test_mobile_menu_without_javascript(ready_client):
    html = ready_client.get("/").text
    assert "<details" in html
    assert "<script" not in html


def test_pages_without_sidebar_use_the_stylesheet(client):
    for path in ("/setup",):
        html = client.get(path).text
        assert "/static/css/reprintbuddy.css" in html
        assert "sidebar-nav" not in html


def test_setup_bambuddy_page_has_no_sidebar(admin_client):
    html = admin_client.get("/setup/bambuddy").text
    assert "/static/css/reprintbuddy.css" in html
    assert "sidebar-nav" not in html


def test_login_page_has_no_sidebar(admin_client):
    admin_client.cookies.clear()
    html = admin_client.get("/login").text
    assert "/static/css/reprintbuddy.css" in html
    assert "sidebar-nav" not in html


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_page_declares_language(ready_client, lang):
    with ready_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", lang)
    assert f'<html lang="{lang}">' in ready_client.get("/").text
