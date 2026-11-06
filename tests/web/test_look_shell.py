"""The shell of the new look (Spec 10.1): neutral grey tokens, full width, dense tables and the
sidebar that folds away (cookie, with and without JavaScript)."""

import re

import pytest

from reprintbuddy import __version__

CSS = "/static/css/reprintbuddy.css"


def css_text(client):
    return client.get(CSS).text


@pytest.mark.parametrize(
    "token",
    [
        "--bg: #161718",
        "--menu: #1d1e20",
        "--surface: #222326",
        "--line: #2c2d31",
        "--text: #ececec",
        "--text-2: #a1a1aa",
        "--muted: #8b8d93",
        "--accent: #00ae42",
    ],
)
def test_tokens_match_spec(ready_client, token):
    assert token in css_text(ready_client)


def test_no_720_limit(ready_client):
    css = css_text(ready_client)
    assert "max-width: 720px" not in css
    assert "max-width: 960px" not in css


def test_table_heads_small_caps(ready_client):
    css = css_text(ready_client)
    rule = re.search(r"\nth \{[^}]*\}", css)
    assert rule and "text-transform: uppercase" in rule.group(0)


def test_sidebar_shows_version(ready_client):
    html = ready_client.get("/").text
    sidebar = re.search(r'<aside class="sidebar">.*?</aside>', html, re.S).group(0)
    mobile = re.search(r'<details class="mobile-menu">.*?</details>', html, re.S).group(0)
    assert f"v{__version__}" in sidebar
    assert f"v{__version__}" in mobile


def test_collapse_without_js(ready_client):
    response = ready_client.get("/ui/sidebar?collapsed=1&next=/parts")
    assert response.status_code == 303
    assert response.headers["location"] == "/parts"
    assert ready_client.cookies.get("rb_sidebar") == "collapsed"
    assert 'class="app sidebar-collapsed"' in ready_client.get("/parts").text
    response = ready_client.get("/ui/sidebar?collapsed=0&next=/parts")
    assert response.status_code == 303
    assert ready_client.cookies.get("rb_sidebar") is None
    assert 'class="app"' in ready_client.get("/parts").text


def test_expanded_link_goes_to_collapse(ready_client):
    html = ready_client.get("/parts?q=x").text
    assert "/ui/sidebar?collapsed=1&amp;next=/parts%3Fq%3Dx" in html


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example", "/\\evil", ""])
def test_collapse_next_is_local(ready_client, target):
    response = ready_client.get("/ui/sidebar", params={"collapsed": "1", "next": target})
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_collapse_needs_login(ready_client):
    ready_client.cookies.clear()
    response = ready_client.get("/ui/sidebar?collapsed=1&next=/parts")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert "rb_sidebar" not in response.headers.get("set-cookie", "")
