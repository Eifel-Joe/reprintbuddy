"""Logo and favicon: served without login, linked from every page, wordmark in the layout."""

from pathlib import Path

import pytest
from conftest import finish_admin_setup, make_client

from reprintbuddy import __version__
from reprintbuddy.web import app as web_app

IMG = Path(web_app.__file__).parent / "static" / "img"

FILES = {
    "logo.svg": "image/svg+xml",
    "favicon.svg": "image/svg+xml",
    "favicon-16.png": "image/png",
    "favicon-32.png": "image/png",
    "favicon-48.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "logo-512.png": "image/png",
}

SVG_LINK = f'<link rel="icon" type="image/svg+xml" href="/static/img/favicon.svg?v={__version__}">'
PNG_LINK = f'<link rel="icon" type="image/png" sizes="32x32" href="/static/img/favicon-32.png?v={__version__}">'
APPLE_LINK = f'<link rel="apple-touch-icon" href="/static/img/apple-touch-icon.png?v={__version__}">'


@pytest.mark.parametrize("name,media_type", FILES.items())
def test_logo_files_are_served(client, name, media_type):
    """Also before the setup: the first page a person sees already has its icon."""
    response = client.get(f"/static/img/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == media_type
    assert response.content == (IMG / name).read_bytes()


def test_the_svg_files_are_neutral_shapes_only():
    for name in ("logo.svg", "favicon.svg"):
        text = (IMG / name).read_text(encoding="utf-8")
        assert text.lstrip().startswith("<svg")
        assert "<script" not in text and "href=" not in text  # nothing that loads or runs anything


def test_favicon_ico_is_open_before_the_setup(client):
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "image/png"
    assert response.content == (IMG / "favicon-48.png").read_bytes()


def test_favicon_ico_is_open_without_login(ready_client):
    ready_client.cookies.clear()
    response = ready_client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.content == (IMG / "favicon-48.png").read_bytes()


def test_favicon_ico_is_open_during_the_bambuddy_step(admin_client):
    assert admin_client.get("/favicon.ico").status_code == 200  # no redirect to the setup


def test_every_layout_links_the_icons(tmp_path, ready_client):
    admin_client = finish_admin_setup(make_client(tmp_path / "admin.db"))  # setup step 2 still open
    pages = {"app": None}
    pages["setup admin"] = make_client(tmp_path / "fresh.db").get("/setup")
    pages["setup bambuddy"] = admin_client.get("/setup/bambuddy/user")
    pages["setup key"] = admin_client.get("/setup/bambuddy/key")
    pages["setup connect"] = admin_client.get("/setup/bambuddy/connect")
    pages["app"] = ready_client.get("/")
    pages["settings"] = ready_client.get("/settings")
    ready_client.cookies.clear()
    pages["login"] = ready_client.get("/login")
    for label, response in pages.items():
        assert response.status_code == 200, label
        for link in (SVG_LINK, PNG_LINK, APPLE_LINK):
            assert link in response.text, (label, link)
        assert 'href="data:,"' not in response.text, label  # the old placeholder icon is gone


def test_sidebar_has_the_symbol_and_the_two_colour_wordmark(ready_client):
    html = ready_client.get("/").text
    assert f'<img class="brand-mark" src="/static/img/logo.svg?v={__version__}" alt="" width="30" height="30">' in html
    assert '<span class="brand-text"><span class="brand-reprint">Reprint</span><span class="brand-buddy">buddy</span></span>' in html


def test_login_and_setup_show_the_big_mark(tmp_path, ready_client):
    ready_client.cookies.clear()
    for html in (ready_client.get("/login").text, make_client(tmp_path / "fresh.db").get("/setup").text):
        assert f'class="brand-mark brand-mark-large" src="/static/img/logo.svg?v={__version__}"' in html
        assert '<span class="brand-reprint">Reprint</span><span class="brand-buddy">buddy</span>' in html


def test_wordmark_colours_are_in_the_stylesheet(client):
    css = client.get("/static/css/reprintbuddy.css").text
    assert ".brand-buddy" in css and "#00c64d" in css
    assert ".brand-reprint" in css and "var(--text)" in css


def test_the_mobile_menu_has_the_mark_too(ready_client):
    html = ready_client.get("/").text
    summary = html[html.index("<summary>") : html.index("</summary>")]
    assert "/static/img/logo.svg" in summary
