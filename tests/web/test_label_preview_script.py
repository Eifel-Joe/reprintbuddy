"""The preview of the label asks the server again and again (settings: on every tick). When an answer
does not come (network, server) the old picture and the old notes stay; the page must say so (p138)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from reprintbuddy.web.i18n import translate

HERE = Path(__file__).parent
SCRIPT = HERE.parents[1] / "src" / "reprintbuddy" / "web" / "static" / "js" / "labels.js"
NODE = shutil.which("node")


def en(key):
    return translate("en", key)


def de(key):
    return translate("de", key)


def test_the_failure_note_is_in_both_languages_and_says_the_preview_could_not_be_updated():
    assert en("labels.preview_failed") != "labels.preview_failed" and "could not be updated" in en("labels.preview_failed")
    assert de("labels.preview_failed") != "labels.preview_failed" and "konnte nicht aktualisiert werden" in de("labels.preview_failed")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_script_shows_the_note_when_an_answer_fails_and_hides_it_when_the_preview_loads_again():
    done = subprocess.run([NODE, str(HERE / "label_preview_harness.js"), str(SCRIPT)], capture_output=True, text=True, timeout=60, check=True)
    result = json.loads(done.stdout)
    # settings: fine, then a failed answer (the old picture and note stay, the failure is told), then fine again
    assert result["settings_fail_then_recover"] == [
        {"failed": False, "rows": True, "src": "blob:x"},
        {"failed": True, "rows": True, "src": "blob:x"},
        {"failed": False, "rows": False, "src": "blob:x"},
    ]
    # an old request that fails after a newer one has been answered says nothing
    assert result["settings_old_failure_is_ignored"][-1]["failed"] is False
    # the article's page asks once: when that fails the note is there too (the browser tries the picture itself)
    assert result["article_first_load_fails"] == [{"failed": True, "rows": False, "src": "/articles/1/label.png"}]
