"""The browser side of the live channel: static/js/live.js and what the pages give it.

The script itself runs only in a browser; these tests hold what the server can see: which pages
load it, the hidden notices it shows, and that it carries no texts or addresses of its own and
speaks the names the server uses (Spec 10.5, plan decisions 3 to 8).
"""

import re
from pathlib import Path

from conftest import csrf_of, form_token_of
from factories import make_part

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


def test_a_page_shown_for_a_post_still_has_the_notice(ready_client):
    """A failed booking is shown again under the POST address (no region source): live.js must
    be able to show the notice there instead of reloading, which would send the form again."""
    with ready_client.app.state.sessionmaker.begin() as s:
        part_id = make_part(s, name="Insert M4").id
    data = {
        "csrf_token": csrf_of(ready_client, "/bookings"),
        "form_token": form_token_of(ready_client, "/bookings", "/bookings/part-receipt"),
        "part_id": part_id,
        "qty": "0",
    }
    response = ready_client.post("/bookings/part-receipt", data=data)
    assert response.status_code == 200 and 'data-live-src=""' in response.text
    assert "data-live-reload" in notice(response.text)
    assert SCRIPT_TAG in response.text


def function_body(text, name):
    match = re.search(r"\n  function " + name + r"\([^)]*\) \{\n(.*?)\n  \}\n", text, re.S)
    assert match, f"function {name} missing"
    return match.group(1)


def test_live_script_reloads_only_where_the_page_has_a_get_address():
    """Reloading a page shown for a POST would send the form again (a second booking). Every
    reload goes through one function that refuses when the page has no region source; the
    notice and the click on its button use it, and such a page hides the reload buttons."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count("location.reload(") == 1
    guard = function_body(text, "reloadIfSafe")
    assert re.match(r"\s*if \(!source\) \{\s*return false;", guard), guard
    assert "window.location.reload();" in guard
    assert "reloadIfSafe(true)" in function_body(text, "reloadOrNotice")  # automatic (0.4.1, R4)
    click = re.search(r'addEventListener\("click", function \(event\) \{(.*?)\n  \}\);', text, re.S)
    assert click and "reloadIfSafe()" in click.group(1)
    hide = re.search(r"\n  if \(!source\) \{\n(.*?)\n  \}\n", text, re.S)
    assert hide and 'querySelectorAll("[data-live-reload]")' in hide.group(1)
    assert "hidden = true" in hide.group(1)


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
