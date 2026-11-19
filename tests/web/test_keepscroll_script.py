"""p645 (v1.2.1): after a button, a switch or a saved form the page stays where it was.

Nearly every action is a POST that answers with a redirect, and the browser starts the page it is
sent to at the top. static/js/keepscroll.js remembers the position when a form is sent and sets it
again on the page that comes back. The script runs in a browser; here it runs against a stand-in
page (keepscroll_harness.js, node): what it remembers, when it restores and when it must not.
"""

import re
from pathlib import Path

import pytest
from node_support import SCRIPTS, run_harness

SCRIPT = SCRIPTS / "keepscroll.js"
Y = 640


@pytest.fixture(scope="module")
def result() -> dict:
    return run_harness("keepscroll_harness.js", SCRIPT)


def restored(case: dict, y: float = Y) -> bool:
    return case["calls"] == [f"scrollTo:{y}"] and case["y"] == y


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
    ["get_form", "prevented", "blank_target", "submitter_get", "submitter_blank", "field_named_method_get_form"],
)
def test_what_does_not_load_this_page_again_remembers_nothing(result, case):
    assert result[case]["stored"] is None
    assert untouched(result[case])


def test_a_button_may_turn_a_get_form_into_a_post(result):
    assert result["submitter_post_on_get_form"]["stored"]["y"] == Y


@pytest.mark.parametrize("case", ["submitter_plain", "self_target"])
def test_a_button_without_its_own_method_or_a_target_of_this_tab_leaves_the_form_alone(result, case):
    assert result[case]["stored"]["y"] == Y


def test_a_field_called_target_does_not_hide_the_forms_own_target(result):
    # The target quantity of the article list is a field called "target": `form.target` is that
    # field then, not the attribute. The first version of the script took it for another tab and
    # never remembered the position of the main list (review of 0e1b2d2).
    assert result["field_named_target"]["stored"]["y"] == Y
    assert restored(result["field_named_target"])


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
        "no_redirect",  # a link to the same page, or a POST that was only a download: not the answer
    ],
)
def test_where_the_position_must_not_be_used(result, case):
    assert untouched(result[case]), case


def test_a_short_wait_is_still_in_time(result):
    assert restored(result["not_so_old"])


@pytest.mark.parametrize("case", ["no_navigation_api", "redirect_count_unknown"])
def test_without_the_navigation_numbers_the_page_is_taken_as_the_answer(result, case):
    assert restored(result[case])


@pytest.mark.parametrize(
    "case",
    ["other_path", "other_tab", "anchor_wins", "old", "post_answer", "reload", "back", "no_redirect"],
)
def test_every_page_load_uses_up_the_entry(result, case):
    # else it would wait for the next visit to the same address and move that one
    assert result[case]["left_in_storage"] is False, case


def test_a_page_without_an_entry_is_left_alone(result):
    assert result["no_entry"] == {"calls": [], "error": None}
    assert result["load_only"]["calls"] == []


@pytest.mark.parametrize(
    "name", ["not_json", "y_text", "y_negative", "y_infinite", "no_path", "at_text", "head_text", "array", "null"]
)
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


def test_somebody_who_scrolled_meanwhile_is_not_pulled_back(result):
    case = result["short_page_but_scrolled"]
    assert case["y"] == 120
    assert case["calls"] == ["scrollTo:900"]  # the one try at the start, none after the load


def test_a_page_that_was_long_enough_is_not_moved_again(result):
    assert result["long_page_once"]["calls"] == ["scrollTo:400"]


# --- a message above the content shifts the page ------------------------------------------------

def test_a_message_that_came_in_above_the_content_is_made_up_for(result):
    # the success message of the answer pushes the page head 60 px down: the same row stays put
    assert restored(result["head_moved_down"], Y + 60)


def test_a_message_that_is_gone_is_made_up_for_too(result):
    assert restored(result["head_moved_up"], Y - 60)


def test_at_the_top_the_message_stays_in_view(result):
    assert result["head_moved_at_top"]["y"] == 0


def test_the_correction_never_goes_above_the_page(result):
    assert result["head_moved_beyond_top"]["calls"] == ["scrollTo:0"]


@pytest.mark.parametrize("case", ["head_on_one_page_only", "head_same"])
def test_without_a_head_to_compare_the_plain_position_is_used(result, case):
    assert restored(result[case])


# --- trouble on the page -------------------------------------------------------------------------

def test_an_error_on_the_page_is_brought_into_view_after_the_position(result):
    # restored first, then only as far as needed to see the message ("nearest": nothing if it is there)
    assert result["marker_after_restore"]["calls"] == [f"scrollTo:{Y}", "into:nearest:m"]
    assert result["marker_field_error"]["calls"] == [f"scrollTo:{Y}", "into:nearest:f"]


def test_an_error_in_a_hidden_layout_is_skipped(result):
    # the list draws the table and the tiles; one is hidden, and scrolling to a hidden element does nothing
    assert result["marker_hidden_first"]["calls"] == [f"scrollTo:{Y}", "into:nearest:shown"]
    assert result["marker_only_hidden"]["calls"] == [f"scrollTo:{Y}"]


def test_a_note_that_is_always_on_the_page_is_not_trouble_on_a_page_that_was_restored(result):
    assert result["marker_row_on_get_page"]["calls"] == [f"scrollTo:{Y}"]


def test_nothing_moves_for_an_error_on_a_page_that_was_not_restored(result):
    assert result["marker_without_trip"]["calls"] == []
    assert result["marker_but_anchor"]["calls"] == []
    assert result["marker_but_reload"]["calls"] == []


def test_the_page_that_answers_the_post_shows_its_error(result):
    # It starts at the top, as ever, but an error written into a row far below is brought into view.
    assert result["post_answer_with_row_error"]["calls"] == ["into:nearest:r"]
    assert result["post_answer_with_field_error"]["calls"] == ["into:nearest:f"]
    assert result["post_answer_without_trip"]["calls"] == ["into:nearest:r"]
    assert result["post_answer_without_error"]["calls"] == []


# --- the file itself -----------------------------------------------------------------------------

def test_the_script_has_no_texts_or_addresses():
    text = Path(SCRIPT).read_text(encoding="utf-8")
    for word in ("http", "/live", "Gespeichert", "Saved"):
        assert word not in text, word


def test_the_script_reads_attributes_not_the_properties_a_field_can_hide():
    # `form.method`, `form.target`, `form.action` (and the same of a button) are the controls of
    # that name when the form has one; `getAttribute` is always the attribute.
    text = re.sub(r"/\*.*?\*/", "", Path(SCRIPT).read_text(encoding="utf-8"), flags=re.S)  # the code, not the comments
    for property_ in ("form.method", "form.target", "form.action", "submitter.formMethod", "submitter.formTarget"):
        assert property_ not in text, property_
