"""p645 (v1.2.1): every page with a menu gets the script that keeps the position after a form.

The script itself is tested in test_keepscroll_script.py (node). Here: it is on every page that
has forms, before live.js, not on the pages without a menu; no template with a POST form can sit on
a page without it; and the two things the script looks for on a page that wants to be seen
(trouble in the message box, a refused value at its field) are what the pages actually write.
"""

import re
from pathlib import Path

import pytest
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article

from reprintbuddy import __version__
from reprintbuddy.web import i18n

WEB = Path(i18n.__file__).parent
TEMPLATES = WEB / "templates"
SCRIPT = WEB / "static" / "js" / "keepscroll.js"
TAG = f'<script src="/static/js/keepscroll.js?v={__version__}" defer></script>'
LIVE_TAG = f'<script src="/static/js/live.js?v={__version__}" defer></script>'

# Pages without a menu (bare.html): sign-in and first-run setup. They load no scripts at all.
BARE = {"login.html", "setup_admin.html", "setup_bambuddy_connect.html", "setup_bambuddy_key.html", "setup_bambuddy_user.html"}
# The tabs of the settings page are included by settings.html, which extends base.html.
SETTINGS_TABS = {"settings_labels.html", "settings_log.html", "settings_notify.html", "settings_ops.html"}

POST_FORM = re.compile(r"<form\b[^>]*\bmethod=\"post\"", re.I)


def test_pages_with_a_menu_load_the_script_once_before_live_js(ready_client):
    for path in ("/", "/articles", "/settings", "/queue", "/bookings", "/orders", "/parts", "/reorder"):
        page = ready_client.get(path).text
        assert page.count(TAG) == 1, path
        assert page.index(TAG) < page.index(LIVE_TAG), path


def test_the_script_is_served(ready_client):
    response = ready_client.get(f"/static/js/keepscroll.js?v={__version__}")
    assert response.status_code == 200


def test_a_page_without_a_menu_loads_no_scripts(client):
    page = client.get("/setup", follow_redirects=True).text
    assert "keepscroll.js" not in page


def _post_form_templates():
    return sorted(p for p in TEMPLATES.glob("*.html") if POST_FORM.search(p.read_text(encoding="utf-8")))


def test_every_template_with_a_post_form_is_on_a_page_that_has_the_script():
    """A new page with a form gets the script by extending base.html; one that does not must be
    named here, with the reason."""
    for path in _post_form_templates():
        name = path.name
        if name == "base.html" or name.startswith("_") or name in SETTINGS_TABS or name in BARE:
            continue  # the base itself, a partial or a tab: included by pages that are looked at below
        text = path.read_text(encoding="utf-8")
        assert re.search(r'{%\s*extends\s+"base.html"\s*%}', text), f"{name}: no base.html, no script"


def test_a_partial_with_a_post_form_is_used_by_a_page_that_extends_base():
    pages = [p for p in TEMPLATES.glob("*.html") if not p.name.startswith("_") and p.name not in BARE]
    for path in _post_form_templates():
        name = path.name
        if not name.startswith("_") and name not in SETTINGS_TABS:
            continue
        users = [
            p for p in pages
            if p != path and re.search(rf'["\']{re.escape(name)}["\']', p.read_text(encoding="utf-8"))
        ]
        # `_ui.html` is imported by everything; the others are included by at least one page
        assert users, f"{name} is not used by any page"


# --- what the script looks for on a page --------------------------------------------------------

def test_the_script_looks_for_trouble_in_the_box_and_for_a_field_error():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "[data-flashes] .alert" in text
    assert ".field-error" in text


def test_a_warning_stands_in_the_box_the_script_looks_for(ready_client):
    """A refused automation switch comes back as a warning in the message box of the list."""
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
    token = form_token_of(ready_client, "/articles", f"/articles/{a}/auto")
    data = {"csrf_token": csrf_of(ready_client, "/articles"), "form_token": token, "enabled": "maybe", "next": "/articles"}
    assert ready_client.post(f"/articles/{a}/auto", data=data).status_code == 303
    page = ready_client.get("/articles").text
    box = re.search(r'<div class="flashes" data-flashes[^>]*>.*?</div>', page, re.S).group(0)
    assert 'class="alert"' in box


def test_a_refused_target_stands_at_its_field_as_the_script_expects(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A", min_stock=10, buffer=5).id
    token = form_token_of(ready_client, "/articles", f"/articles/{a}/target")
    data = {"csrf_token": csrf_of(ready_client, "/articles"), "form_token": token, "target": "1", "next": "/articles"}
    assert ready_client.post(f"/articles/{a}/target", data=data).status_code == 303
    page = ready_client.get("/articles").text
    assert 'class="field-error"' in page
    assert page.count(TAG) == 1
    # The field is called "target", which hides `form.target` in the browser: the script must read
    # the attribute (test_keepscroll_script.py), or the main list never keeps its place.
    assert re.search(r'<form class="target-form"[^>]*>.*?<input[^>]*name="target"', page, re.S)
    # and there is a page head to measure a message above the content against
    assert 'class="page-head"' in page


@pytest.mark.parametrize("name", ["keepscroll.js"])
def test_the_script_is_listed_nowhere_else(name):
    """live.js reloads and replaces regions by itself; the position script does not hook into it."""
    live = (WEB / "static" / "js" / "live.js").read_text(encoding="utf-8")
    assert name not in live
