"""Small arrows on every number field (Spec 10.1, p155): one script for all pages."""

import re
from pathlib import Path

import pytest

from node_support import run_harness

HERE = Path(__file__).parent
WEB = HERE.parents[1] / "src" / "reprintbuddy" / "web"
SCRIPT = WEB / "static" / "js" / "spinbox.js"


@pytest.fixture(scope="module")
def result():
    return run_harness("spinbox_harness.js", SCRIPT)


def test_numbers_are_read_with_comma_or_point(result):
    # "1,5" and "1.5" alike; blanks inside are dropped; anything else is no number
    assert result["read"] == [1.5, 1.5, 12, 1000, None, None, None, -3, 4, None, None, None]


def test_a_step_counts_in_the_decimals_of_the_step_and_keeps_to_the_limits(result):
    assert result["step"] == [
        1,      # empty counts from 0: up
        0,      # empty, minimum 0: down stays at 0
        -1,     # empty, no minimum: down goes below 0
        0,      # at the minimum
        0.2,    # 0.1 + 0.1
        0.3,    # 0.2 + 0.1 (no 0.30000000000000004)
        10,     # 9 + 5 is drawn to the maximum
        5,      # 15 - 10
        100,    # beyond the maximum: the maximum
        2.75,
        4,      # a step of 0 counts as 1
    ]
    assert result["decimals"] == [0, 1, 2, 0]


def test_the_value_is_written_in_the_writing_of_the_interface(result):
    # German: a comma; English: a point; a number field always a point (what the browser expects)
    assert result["write"] == ["1,5", "1.5", "1,5", "1.5", "7", "0,3"]


def test_every_number_field_gets_arrows_unless_it_opts_out(result):
    # text number, number, text number without min, opted out, disabled, plain text field
    assert result["wrapped"] == [True, True, True, False, True, False]
    # the arrows are for the pointer: the keyboard has the arrow keys in the field
    assert result["hidden_from_assistive_tech"] == [["true", -1], ["true", -1]]


def test_an_arrow_counts_fires_input_and_change_and_stops_at_a_limit(result):
    start, up, up_at_max, down_twice, down_at_min = result["text"]
    assert start[1:] == ["2", "", [False, False]]
    assert up[1:] == ["3", "input,change", [True, False]]  # at the maximum the up arrow is off
    assert up_at_max[1:] == up[1:]  # nothing counts beyond, no further event
    assert down_twice[1] == "1" and down_twice[3] == [False, True]  # at the minimum the down arrow is off
    assert down_at_min[1:] == down_twice[1:]  # live.js sees a changed form after each count, never one too many


def test_a_number_field_counts_with_a_point_in_the_steps_of_the_field(result):
    assert result["number"] == ["5.5", "6.0"]


def test_an_empty_field_counts_from_zero_and_text_fields_default_to_zero_as_minimum(result):
    assert result["free"] == ["1", "0"]


def test_a_typed_comma_is_read(result):
    assert result["comma_in"] == "3"  # 1,5 + 1, written back with the (no) decimals of the field


def test_the_key_arrows_count_in_a_text_field_but_not_with_a_modifier(result):
    stopped_up, after_up, stopped_down, after_down, ctrl, after_ctrl, other, after_other = result["keys"]
    assert (stopped_up, after_up, stopped_down, after_down) == (True, "3", True, "2")
    assert (ctrl, after_ctrl, other, after_other) == (False, "2", False, "2")
    assert result["number_has_no_key_handler"] is True  # a number field counts by itself: not twice


def test_a_disabled_field_is_not_counted(result):
    assert result["locked"] == ["4", "4", [True, True]]


def test_fields_that_appear_later_are_enhanced_once(result):
    assert result["later"] == [True, 1, True]
    assert result["not_twice"] is True


def test_a_held_arrow_stops_when_another_press_begins_even_if_its_own_end_never_comes(result):
    """The arrow that reached its limit is switched off; a browser may never send it the end of the press.
    Pressing the other arrow ends the first one's repeat, so "up" does not keep counting against "down"."""
    assert result["held_arrow_runs_on"] == "2"


# ---- the pages ---------------------------------------------------------------------------------


def test_every_page_loads_the_script_and_it_is_no_inline_script():
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    assert re.search(r'<script src="/static/js/spinbox\.js\?v=\{\{ app_version \}\}" defer></script>', base)
    assert "document.write" not in SCRIPT.read_text(encoding="utf-8")


def test_every_text_number_field_says_its_minimum_or_opts_out():
    """The arrow of a text field without `min` stops at 0. A field the server accepts below 0 would be locked
    by that; none does today, and every field names its own limit so a new one has to decide."""
    missing = []
    for path in sorted((WEB / "templates").glob("*.html")):
        for tag in re.findall(r"<input\b[^>]*>", path.read_text(encoding="utf-8")):
            if 'inputmode="numeric"' in tag or 'inputmode="decimal"' in tag:
                if " min=" not in tag and "data-no-spin" not in tag:
                    missing.append(f"{path.name}: {tag[:80]}")
    assert missing == []


def test_the_port_is_no_counter():
    settings = (WEB / "templates" / "settings.html").read_text(encoding="utf-8")
    assert re.search(r'name="port"[^>]*data-no-spin', settings)
