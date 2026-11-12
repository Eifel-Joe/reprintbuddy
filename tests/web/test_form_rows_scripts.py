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
