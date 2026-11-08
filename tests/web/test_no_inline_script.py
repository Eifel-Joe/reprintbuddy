"""No template carries a script of its own or an event handler attribute (v0.3.8 p78).

All scripts live in files under static/js and are loaded with `<script src=...>`; a page can then
forbid inline script in a Content-Security-Policy without breaking anything.
"""

import re
from pathlib import Path

import reprintbuddy.web

TEMPLATES = Path(reprintbuddy.web.__file__).parent / "templates"
SCRIPT_TAG = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
HANDLER = re.compile(r"""<[a-z][^>]*\s(on[a-z]+)\s*=""", re.I)
JS_URL = re.compile(r"""(?:href|src|action|formaction)\s*=\s*["']\s*javascript:""", re.I)


def test_templates_exist():
    assert len(list(TEMPLATES.glob("*.html"))) > 20  # the scan below must not pass on an empty folder


def test_every_script_tag_loads_a_file_and_has_no_body():
    problems = []
    for path in sorted(TEMPLATES.glob("*.html")):
        for attrs, body in SCRIPT_TAG.findall(path.read_text(encoding="utf-8")):
            if not re.search(r'\bsrc="/static/js/[\w.-]+\.js(\?[^"]*)?"', attrs) or body.strip():
                problems.append(f"{path.name}: <script{attrs}>{body.strip()[:40]}")
    assert problems == []


def test_no_unclosed_or_stray_script_tag():
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        assert len(re.findall(r"<script\b", text, re.I)) == len(SCRIPT_TAG.findall(text)), path.name


def test_no_event_handler_attributes_or_javascript_urls():
    problems = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        problems += [f"{path.name}: {m.group(1)}=" for m in HANDLER.finditer(text)]
        problems += [f"{path.name}: javascript: URL" for _ in JS_URL.finditer(text)]
    assert problems == []


def test_a_missing_preview_picture_is_removed_by_a_file_not_an_attribute(bb_client):
    """The file choice lists files with a preview; a picture that fails to load must still vanish."""
    page = bb_client.get("/articles/new?folder=1").text
    assert "data-remove-on-error" in page
    assert "onerror" not in page
    assert '<script src="/static/js/thumbs.js' in page
    source = (Path(reprintbuddy.web.__file__).parent / "static" / "js" / "thumbs.js").read_text(encoding="utf-8")
    assert 'addEventListener(\n    "error"' in source and "img[data-remove-on-error]" in source
    assert bb_client.get("/static/js/thumbs.js").status_code == 200
