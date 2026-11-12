"""The form scripts in a stand-in document (form_rows_harness.js): the "more rows" button after the
live channel swapped a form (p392) and drop-downs that leave out what other rows hold (p351)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
JS = HERE.parents[1] / "src" / "reprintbuddy" / "web" / "static" / "js"
NODE = shutil.which("node")


@pytest.fixture(scope="module")
def result():
    if NODE is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [NODE, str(HERE / "form_rows_harness.js"), str(JS / "more_rows.js"), str(JS / "set_form.js"),
         str(JS / "assembled_form.js")],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(done.stdout)


def test_full_part_has_its_button_off_also_after_a_live_swap(result):  # p392
    assert result["full_at_load"] is True
    assert result["full_after_swap"] is True
    assert result["room_after_swap"] is False


ALL = ["", "1", "2", "3", "s9"]


def test_set_form_leaves_out_what_another_row_holds(result):  # p351: iPhone shows hidden options greyed
    at_load, choosing, back, swapped = (result["sets"][k] for k in (
        "at_load", "after_choosing_a_set", "after_taking_it_back", "after_live_swap"))
    assert at_load == [ALL, ["", "2", "3", "s9"], 0]  # nothing stays in a list only hidden
    assert choosing == [["", "1", "2"], ["", "2", "3", "s9"], "1"]  # the set takes article 3 too
    assert back == [ALL, ["", "2", "3", "s9"], "1"]  # back in its old place, the own choice kept
    assert swapped == [ALL, ["", "2", "3", "s9"]]  # a form the live channel swaps in, too


def test_assembled_form_leaves_out_what_another_row_holds(result):  # p351
    assert result["assembled"]["at_load"] == [["", "1", "2", "3"], ["", "2", "3"], ["", "2", "3"]]
    assert result["assembled"]["after_choosing_in_choice_parts"] == [
        ["", "1", "3"], ["", "3"], ["", "2", "3"], "1",
    ]
