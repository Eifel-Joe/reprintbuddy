"""The browser side of the live channel: static/js/live.js and what the pages give it.

The script itself runs only in a browser; these tests hold what the server can see: which pages
load it, the hidden notices it shows, and that it carries no texts or addresses of its own and
speaks the names the server uses (Spec 10.5, plan decisions 3 to 8).
"""

import re
from pathlib import Path

from reprintbuddy import __version__
from reprintbuddy import settings as st
from reprintbuddy.web import i18n

SCRIPT = Path(i18n.__file__).parent / "static" / "js" / "live.js"
SCRIPT_TAG = f'<script src="/static/js/live.js?v={__version__}" defer></script>'


def notice(html):
    match = re.search(r'<p id="live-version"[^>]*>.*?</p>', html, re.S)
    assert match, "page-level notice missing"
    return match.group(0)


def test_app_pages_load_the_live_script_once_deferred(ready_client):
    html = ready_client.get("/").text
    assert html.count("live.js") == 1
    assert SCRIPT_TAG in html
    assert ready_client.get(f"/static/js/live.js?v={__version__}").status_code == 200


def test_bare_pages_have_no_live_script(client, admin_client):
    fresh = client.get("/setup").text  # nothing set up yet
    assert "live.js" not in fresh and "data-live=" not in fresh
    admin_client.cookies.clear()
    login = admin_client.get("/login").text
    assert "live.js" not in login and "data-live=" not in login


def test_page_level_notice_is_hidden_and_translated(ready_client):
    english = notice(ready_client.get("/").text)
    assert re.match(r'<p id="live-version"[^>]*\bhidden\b', english)
    assert 'class="live-stale"' in english and 'role="status"' in english
    assert "This has changed in the meantime." in english
    assert re.search(r'<button type="button" class="btn-link" data-live-reload>Reload</button>', english)
    with ready_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    german = notice(ready_client.get("/").text)
    assert "Hat sich inzwischen geändert." in german
    assert "Neu laden" in german


def test_live_script_has_no_texts_or_addresses():
    text = SCRIPT.read_text(encoding="utf-8")
    for word in ("http", "/live", "Reload", "geändert"):
        assert word not in text, word


def test_live_script_speaks_the_servers_names():
    text = SCRIPT.read_text(encoding="utf-8")
    for name in (
        "X-Live-Regions",
        "data-live-region",
        "data-live-topics",
        "data-live-watch",
        "data-live-src",
        "data-live-state",
        '"hello"',
        '"change"',
        '"reload"',
    ):
        assert name in text, name


def test_stale_notice_macro_is_hidden_and_translated(ready_client):
    """`ui.live_stale()`, the first child of a form that watches a topic."""
    template = ready_client.app.state.templates.env.from_string(
        '{% import "_ui.html" as ui with context %}{{ ui.live_stale() }}'
    )
    html = template.render(t=lambda key, **kw: i18n.translate("en", key, **kw))
    assert html.strip() == (
        '<p class="live-stale" hidden role="status">This has changed in the meantime. '
        '<button type="button" class="btn-link" data-live-reload>Reload</button></p>'
    )
