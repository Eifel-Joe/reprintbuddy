"""p717: the switch of the column "Verbrauch" on the filament page (Spec 9.3).

static/js/filament_usage.js runs in a browser; here it runs against a stand-in page
(filament_usage_harness.js, node): it shows the switch, applies the remembered view, keeps a new choice,
survives blocked storage and applies the choice again after live.js swapped the list region.
"""

import pytest
from node_support import SCRIPTS, run_harness

SCRIPT = SCRIPTS / "filament_usage.js"
BOTH = lambda view: [view, view]  # noqa: E731 - the head span and the row span of one view


@pytest.fixture(scope="module")
def result() -> dict:
    return run_harness("filament_usage_harness.js", SCRIPT)


def test_the_first_visit_shows_the_switch_on_the_default(result):
    case = result["first_visit"]
    assert case["error"] is None
    assert case["start"] == {"choiceShown": True, "selected": "avg30", "shown": BOTH("avg30")}
    assert case["stored"] is None


def test_the_remembered_view_is_shown(result):
    assert result["remembered"]["start"] == {"choiceShown": True, "selected": "total", "shown": BOTH("total")}


def test_an_unknown_remembered_view_keeps_the_default(result):
    assert result["unknown_stored"]["start"]["shown"] == BOTH("avg30")


def test_a_choice_is_shown_and_kept(result):
    case = result["chosen"]
    assert case["afterChange"]["shown"] == BOTH("today")
    assert case["stored"] == "today"


def test_blocked_storage_still_switches(result):
    case = result["blocked_storage"]
    assert case["error"] is None
    assert case["start"]["choiceShown"] is True and case["start"]["shown"] == BOTH("avg30")
    assert case["afterChange"]["shown"] == BOTH("week")


def test_the_choice_comes_back_after_a_live_refresh(result):
    case = result["swapped"]
    assert case["beforeObserver"]["shown"] == BOTH("avg30") and case["beforeObserver"]["choiceShown"] is False
    assert case["afterSwap"] == {"choiceShown": True, "selected": "year", "shown": BOTH("year")}


def test_a_page_without_the_list_is_left_alone(result):
    assert result["no_list"] == {"error": None, "start": None, "stored": None}
