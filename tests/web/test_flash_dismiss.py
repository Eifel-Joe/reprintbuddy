"""Messages at the top of a page go away by themselves (p161, v0.3.13).

Good news disappears after a few seconds, trouble stays until it is dismissed. Both queues of
messages are gone: every message comes from web/flash.py and stands in one `data-flashes` box that
static/js/flash.js looks after. The script itself runs in a browser; here the server side is held
(the box, the texts it takes from the page) and the script is run against a stand-in page by node.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_part

from reprintbuddy import __version__
from reprintbuddy.models import ArticlePart
from reprintbuddy.web import deps, i18n
from reprintbuddy.web.i18n import translate

HERE = Path(__file__).parent
SCRIPT = Path(i18n.__file__).parent / "static" / "js" / "flash.js"
SCRIPT_TAG = f'<script src="/static/js/flash.js?v={__version__}" defer></script>'
NODE = shutil.which("node")
REGIONS = {"X-Live-Regions": "journal"}


def en(key, **params):
    return translate("en", key, **params)


def box_of(page):
    """The (first) message box of a page, whole."""
    match = re.search(r'<div class="flashes" data-flashes[^>]*>.*?</div>', page, re.S)
    assert match, "no message box on the page"
    return match.group(0)


def save_language(client):
    data = {"csrf_token": csrf_of(client, "/settings"), "language": "en"}
    assert client.post("/settings/language", data=data).status_code == 303


def stock_in_with_missing_part(client):
    """A booking with good news and a warning (a part is short)."""
    with sm_of(client).begin() as s:
        a = make_article(s, name="Template A").id
        p = make_part(s, name="Insert M4", stock=1).id
        s.add(ArticlePart(article_id=a, part_id=p, qty_per_unit=2))
    data = {
        "csrf_token": csrf_of(client, "/bookings"),
        "form_token": form_token_of(client, "/bookings", "/bookings/stock-in"),
        "article_id": a, "qty": "2", "consume_parts": "on",
    }
    assert client.post("/bookings/stock-in", data=data).status_code == 303


def test_a_message_stands_in_the_box_the_script_looks_after(ready_client):
    save_language(ready_client)
    page = ready_client.get("/settings").text
    box = box_of(page)
    assert '<p class="flash" role="status">' + en("settings.saved") + "</p>" in box
    assert f'data-label-close="{en("flash.close")}"' in box
    assert page.count(SCRIPT_TAG) == 1
    assert ready_client.get(f"/static/js/flash.js?v={__version__}").status_code == 200


def test_good_news_and_trouble_stand_together_in_the_box(ready_client):
    stock_in_with_missing_part(ready_client)
    box = box_of(ready_client.get("/bookings").text)
    assert '<p class="flash" role="status">' in box
    assert '<p class="alert" role="alert">' in box


def test_the_close_label_is_german_in_german(ready_client):
    save_language(ready_client)
    assert ready_client.get("/settings").text  # the message is used up
    data = {"csrf_token": csrf_of(ready_client, "/settings"), "language": "de"}
    assert ready_client.post("/settings/language", data=data).status_code == 303
    box = box_of(ready_client.get("/settings").text)
    assert f'data-label-close="{translate("de", "flash.close")}"' in box
    assert translate("de", "flash.close") != translate("en", "flash.close")


def test_a_page_without_messages_has_no_box(ready_client):
    assert "data-flashes" not in ready_client.get("/settings").text


def test_the_settings_page_has_no_notice_of_its_own_any_more(ready_client):
    # One queue: the message of the settings page is the same kind as every other (p161).
    assert not hasattr(deps, "flash") and not hasattr(deps, "FLASH_KEY")
    save_language(ready_client)
    page = ready_client.get("/settings").text
    assert 'class="notice" role="status">' + en("settings.saved") not in page
    assert page.count(en("settings.saved")) == 1


def test_a_region_request_brings_no_message_box(ready_client):
    save_language(ready_client)
    response = ready_client.get("/journal", headers=REGIONS)
    assert response.status_code == 200 and "x-live-regions" in response.headers
    assert "data-flashes" not in response.text
    assert box_of(ready_client.get("/settings").text)  # still waiting for the page that shows it


def test_the_script_has_no_texts_or_addresses():
    text = SCRIPT.read_text(encoding="utf-8")
    for word in ("http", "/live", "Dismiss", "schließen", "Meldung"):
        assert word not in text, word
    assert "data-label-close" in text  # the label comes from the page
    assert "prefers-reduced-motion" in text and "visibilitychange" in text


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_script_behaves():
    done = subprocess.run(
        [NODE, str(HERE / "flash_script_harness.js"), str(SCRIPT)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    result = json.loads(done.stdout)
    # good news: still there just before the delay, gone after it
    assert result["success_before"] == {"present": True, "leaving": False}
    assert result["success_after"] == {"present": False, "leaving": False}
    # with reduced motion: no fade, gone at once after the delay
    assert result["reduced_after"] == {"present": False, "leaving": False}
    # the fade is the in-between state; the element goes a moment later
    assert result["fading_mid"] == {"present": True, "leaving": True}
    # trouble stays however long we wait, and has a button with the label of the page
    assert result["alert_after_long"] == {"present": True, "button": "Dismiss message"}
    assert result["alert_button_removes"] is True
    assert result["focus_goes_to_main"] is True  # not to the top of the page after the dismiss
    # a screen reader says the good news from a hidden live region soon after the load, not only
    # when its reading cursor gets there (the message itself is gone after 8 s)
    assert result["announce_before"] == ""  # the region is there, empty, until a moment after the load
    assert result["announce_after"] == {"role": "status", "live": "polite", "cls": "sr-only", "text": "Saved."}
    assert result["announce_only_good_news"] is True
    # the keyboard focus holds the clock like the pointer; with both on it, both have to let go
    assert result["focus_paused"] == {"present": True}
    assert result["focus_and_pointer_paused"] == {"present": True}
    assert result["focus_and_pointer_released"] == {"present": False}
    # the pointer over the message holds the clock; leaving lets the rest run
    assert result["hover_paused"] == {"present": True}
    assert result["hover_resumed_early"] == {"present": True}
    assert result["hover_resumed_end"] == {"present": False}
    # a hidden tab does not run the clock: the message is seen first
    assert result["hidden_tab_waiting"] == {"present": True}
    assert result["visible_again_end"] == {"present": False}
    # no box, no error
    assert result["no_box_ok"] is True
