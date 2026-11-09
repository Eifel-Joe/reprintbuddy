"""v0.3.14 p178: one description of the print file that is taken, in the messages, the README and the docs.

The upload takes every sliced 3MF file: a `.3mf` or `.gcode.3mf` with G-code inside (Spec 5.1a; the name
check is `printfile.upload_name`, the content check looks for G-code). The messages once said
"(.3mf or .gcode.3mf)", the hint and the README only ".gcode.3mf".
"""

import json
import re
from pathlib import Path

import pytest

import reprintbuddy
from reprintbuddy.domain.printfile import upload_name

ROOT = Path(__file__).resolve().parents[1]
LOCALES = Path(reprintbuddy.__file__).parent / "locales"
# The kinds of file, as every text names them (the text round it differs per language).
KINDS = {"en": "(.3mf or .gcode.3mf", "de": "(.3mf oder .gcode.3mf"}
KEYS = (
    "upload.file_hint",
    "upload.file_hint_relink",
    "upload.error.not_3mf",
    "upload.error.not_sliced",
    "articles.not_sliced",
    "articles.hidden_unsliced",
)


def test_the_name_check_takes_both_kinds():
    assert upload_name("plate.3mf") == "plate.3mf"
    assert upload_name("plate.gcode.3mf") == "plate.gcode.3mf"


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("key", KEYS)
def test_every_message_names_both_kinds_of_file(lang, key):
    texts = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    assert KINDS[lang] in texts[key], texts[key]


def _docs():
    files = [ROOT / "README.md", ROOT / "README.de.md", *(ROOT / "docs").rglob("*.md")]
    assert len(files) > 20
    return files


def test_no_line_of_readme_or_docs_names_only_gcode_3mf():
    only = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in _docs()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if ".gcode.3mf" in line and not re.search(r"`\.3mf`", line)
    ]
    assert not only, f"these lines name only `.gcode.3mf`; name `.3mf` too: {only}"
