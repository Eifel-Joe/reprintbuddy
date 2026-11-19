"""p645 (v1.2.1): after a button, a switch or a saved form the page stays where it was.

Nearly every action is a POST that answers with a redirect, and the browser starts the page it is
sent to at the top. static/js/keepscroll.js remembers the position when a form is sent and sets it
again on the page that comes back. The script runs in a browser; here it runs against a stand-in
page (keepscroll_harness.js, node): what it remembers, when it restores and when it must not.
"""

from pathlib import Path

import pytest
from node_support import SCRIPTS, run_harness

SCRIPT = SCRIPTS / "keepscroll.js"
Y = 640


@pytest.fixture(scope="module")
def result() -> dict:
    return run_harness("keepscroll_harness.js", SCRIPT)


def restored(case: dict) -> bool:
    return case["calls"] == [f"scrollTo:{Y}"] and case["y"] == Y


def untouched(case: dict) -> bool:
    return case["calls"] == [] and case["y"] == 0


# --- remembering ---------------------------------------------------------------------------------

def test_a_post_form_remembers_where_the_page_was(result):
    stored = result["basic"]["stored"]
    assert stored["path"] == "/articles" and stored["tab"] == "" and stored["y"] == Y
    assert result["basic"]["stored_at_is_number"] is True


def test_the_settings_tab_is_remembered_with_the_position(result):
    assert result["tab_stored"]["stored"]["tab"] == "general"


@pytest.mark.parametrize(
    "case",
    ["get_form", "prevented", "blank_target", "submitter_get", "submitter_blank"],
)
def test_what_does_not_load_this_page_again_remembers_nothing(result, case):
    assert result[case]["stored"] is None
    assert untouched(result[case])


def test_a_button_may_turn_a_get_form_into_a_post(result):
    assert result["submitter_post_on_get_form"]["stored"]["y"] == Y


def test_a_button_without_its_own_method_leaves_the_form_alone(result):
    assert result["submitter_plain"]["stored"]["y"] == Y


# --- restoring -----------------------------------------------------------------------------------

def test_the_page_that_comes_back_is_set_to_the_old_position(result):
    assert restored(result["basic"])


def test_the_position_is_used_once(result):
    assert result["basic"]["left_in_storage"] is False


def test_the_settings_tab_comes_back_to_its_position(result):
    assert restored(result["tab_stored"])


def test_a_filter_or_a_view_that_changed_does_not_matter(result):
    # the same page, another search or layout: the position still helps more than the top
    assert restored(result["other_query_ok"])
    assert restored(result["extra_query_next_to_tab_ok"])


def test_a_fraction_of_a_pixel_is_fine(result):
    assert result["fraction"]["calls"] == ["scrollTo:640.6"]


def test_a_page_at_the_top_stays_there(result):
    assert result["zero"]["y"] == 0


@pytest.mark.parametrize(
    "case",
    [
        "other_path",  # the action led to another page (a new article): that page starts at the top
        "other_tab",  # another tab of the settings is another page
        "tab_appears",
        "anchor_wins",  # an address with `#…` names its own place
        "old",  # nobody comes back after sixteen seconds with the same intention
        "post_answer",  # a page that answers the POST itself shows an error: the top
        "reload",  # the browser restores a reload itself
        "back",  # and a step back, too
    ],
)
def test_where_the_position_must_not_be_used(result, case):
    assert untouched(result[case]), case


def test_a_short_wait_is_still_in_time(result):
    assert restored(result["not_so_old"])


def test_without_the_navigation_api_the_page_is_taken_as_new(result):
    assert restored(result["no_navigation_api"])


@pytest.mark.parametrize(
    "case",
    ["other_path", "other_tab", "anchor_wins", "old", "post_answer", "reload", "back"],
)
def test_every_page_load_uses_up_the_entry(result, case):
    # else it would wait for the next visit to the same address and move that one
    assert result[case]["left_in_storage"] is False, case


def test_a_page_without_an_entry_is_left_alone(result):
    assert result["no_entry"] == {"calls": [], "error": None}
    assert result["load_only"]["calls"] == []


@pytest.mark.parametrize("name", ["not_json", "y_text", "y_negative", "y_infinite", "no_path", "at_text", "array", "null"])
def test_a_damaged_entry_is_dropped_without_a_word(result, name):
    case = result["damaged"][name]
    assert untouched(case), name
    assert case["errors"] == [], name
    assert case["left_in_storage"] is False, name


def test_a_blocked_storage_does_not_break_the_page(result):
    assert result["storage_blocked"] == {"errors": [], "calls": []}


def test_a_page_that_is_still_short_is_set_again_after_the_load(result):
    case = result["short_page"]
    assert case["before"] < 900  # the browser could not go that far yet
    assert case["after"] == 900


def test_a_page_that_was_long_enough_is_not_moved_again(result):
    assert result["long_page_once"]["calls"] == ["scrollTo:400"]


# --- trouble on the page -------------------------------------------------------------------------

def test_an_error_on_the_page_is_brought_into_view_after_the_position(result):
    # restored first, then only as far as needed to see the message ("nearest": nothing if it is there)
    assert result["marker_after_restore"]["calls"] == [f"scrollTo:{Y}", "into:nearest"]


def test_nothing_moves_for_an_error_on_a_page_that_was_not_restored(result):
    assert result["marker_without_trip"]["calls"] == []
    assert result["marker_but_anchor"]["calls"] == []
    assert result["marker_but_post_answer"]["calls"] == []


# --- the file itself -----------------------------------------------------------------------------

def test_the_script_has_no_texts_or_addresses():
    text = Path(SCRIPT).read_text(encoding="utf-8")
    for word in ("http", "/live", "Gespeichert", "Saved"):
        assert word not in text, word
