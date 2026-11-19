"""static/js/zone.js (Spec 10.5a): every page tells the server the browser's zone, and loads itself once
more when it was computed in another one - never with a changed form, never as the answer to a form,
never without a cookie that holds (no loop). The harness runs the script in node (skipped without node).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from node_support import SCRIPTS, run_harness
from reprintbuddy import __version__
from reprintbuddy.web import regions, zone

SCRIPT = SCRIPTS / "zone.js"
YEAR = "max-age=31536000"


@pytest.fixture(scope="module")
def result():
    return run_harness("zone_script_harness.js", SCRIPT)


def zone_cookie(case):
    return [w for w in case["written"] if w.startswith(zone.COOKIE + "=")]


def test_a_page_in_another_zone_is_loaded_once_more(result):
    case = result["differs"]
    assert case["error"] is None and case["reloads"] == 1
    assert zone_cookie(case) == [f"{zone.COOKIE}=Europe/Berlin; path=/; {YEAR}; SameSite=Lax"]
    assert case["jar"][regions.AUTO_RELOAD_COOKIE] == "1"  # not an opening of the page (Spec 10.2)


def test_a_page_in_the_browsers_zone_stays(result):
    case = result["same"]
    assert case["reloads"] == 0 and case["jar"][zone.COOKIE] == "Europe/Berlin"
    assert regions.AUTO_RELOAD_COOKIE not in case["jar"]


def test_no_loop_when_the_server_keeps_its_own_zone(result):
    """The request carried the browser's zone and the page still came in another: loading it again
    would bring the same page again, for ever."""
    assert result["refused"]["reloads"] == 0


def test_a_new_zone_after_travelling_is_taken_once(result):
    case = result["travelled"]
    assert case["reloads"] == 1 and case["jar"][zone.COOKIE] == "America/New_York"


@pytest.mark.parametrize("name", ["changed_text", "changed_box", "changed_select", "changed_marked"])
def test_no_reload_with_a_changed_form(result, name):
    case = result[name]
    assert case["error"] is None and case["reloads"] == 0
    assert zone_cookie(case)  # the cookie is written all the same: the next page is right


def test_a_form_as_the_page_came_is_no_reason_to_stay(result):
    assert result["unchanged_form"]["reloads"] == 1


def test_no_reload_of_the_answer_to_a_form(result):
    """It would send the form a second time: a second booking."""
    case = result["form_answer"]
    assert case["reloads"] == 0 and zone_cookie(case)


def test_no_reload_while_the_page_shows_a_message(result):
    """The page after a saved form carries its message once: loaded again, "Booked" would be gone, and
    somebody might book a second time. The next page is in the right zone anyway."""
    case = result["flashes"]
    assert case["reloads"] == 0 and zone_cookie(case)


@pytest.mark.parametrize("name", ["blocked", "throws"])
def test_no_reload_when_the_cookie_does_not_hold(result, name):
    case = result[name]
    assert case["error"] is None and case["reloads"] == 0


def test_the_sign_in_page_reloads_without_the_mark(result):
    """No live.js there clears it, and the page after signing in (a scanned code: the orders) would not count
    as opened for half a minute."""
    case = result["bare"]
    assert case["reloads"] == 1 and regions.AUTO_RELOAD_COOKIE not in case["jar"]
    assert not [w for w in case["written"] if w.startswith(regions.AUTO_RELOAD_COOKIE)]


def test_secure_over_https(result):
    assert zone_cookie(result["https"]) == [f"{zone.COOKIE}=Europe/Berlin; path=/; {YEAR}; SameSite=Lax; Secure"]


@pytest.mark.parametrize("name", ["odd_zone", "no_zone", "no_intl"])
def test_nothing_without_a_plain_zone_name(result, name):
    case = result[name]
    assert case["error"] is None and case["written"] == [] and case["reloads"] == 0


def test_the_names_in_the_script_are_the_servers():
    text = SCRIPT.read_text(encoding="utf-8")
    assert f'"{zone.COOKIE}"' in text and f'"{regions.AUTO_RELOAD_COOKIE}"' in text
    assert re.search(r"\{1,%d\}" % zone.MAX_LENGTH, text)  # the same longest name as the server's check


TAG = f'<script src="/static/js/zone.js?v={__version__}" defer></script>'


def test_a_page_with_the_menu_loads_the_script_after_the_live_script(ready_client):
    """After live.js, which clears the mark of an automatic reload as soon as it runs."""
    page = ready_client.get("/").text
    assert TAG in page and page.index("/static/js/live.js") < page.index("/static/js/zone.js")


def test_the_pages_without_the_menu_load_it_too(client):
    """Outside the block the pages without the menu replace: the first setup page, before any account."""
    page = client.get("/setup").text
    assert TAG in page and "/static/js/live.js" not in page
