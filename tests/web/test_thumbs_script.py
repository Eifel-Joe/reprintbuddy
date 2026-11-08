"""`thumbs.js` removes a preview picture that cannot be loaded, also one that failed before it ran (v0.3.9 p120).

There is no JavaScript test setup in this project, so the script is read as text; the behaviour itself was
measured in a browser (see the report of v0.3.9 part B):

- a lazy picture that had failed (404) before the script ran: `complete === true`, `naturalWidth === 0`,
  `currentSrc` set, and the `error` event long gone: it stayed behind as an empty box;
- a lazy picture that has not started to load (far below the visible part): `complete === false`,
  `currentSrc === ""`: it must stay, its own `error` event comes later.
"""

import re
from pathlib import Path

import reprintbuddy

SCRIPT = (Path(reprintbuddy.__file__).parent / "web/static/js/thumbs.js").read_text(encoding="utf-8")


def _code():
    return re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.S)


def test_a_failed_picture_is_found_whether_or_not_it_is_lazy():
    code = _code()
    assert "complete" in code and "naturalWidth === 0" in code
    assert '"lazy"' not in code and "'lazy'" not in code, "a lazy picture that already failed must be swept too"


def test_a_picture_that_has_not_started_loading_is_left_alone():
    """`complete` alone is not enough in every browser: an unstarted lazy picture has no `currentSrc`."""
    assert "currentSrc" in _code()


def test_the_sweep_runs_once_when_the_script_starts_and_the_error_listener_stays():
    code = _code()
    assert 'addEventListener(\n    "error"' in code or 'addEventListener("error"' in code
    assert "sweep()" in code and "DOMContentLoaded" in code
