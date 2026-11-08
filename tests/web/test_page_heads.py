"""Every page head has a subtitle (v0.3.8 p72, the sister path of the restart page).

`ui.page_head(icon, title, subtitle)`: a call without the third argument draws a title alone, which looks
unfinished next to the other pages. The pages that have no page head at all are the sign-in and setup cards
(templates `bare.html` family): they carry a plain heading in a small centred card, by design.
"""

import re
from pathlib import Path

import reprintbuddy.web

TEMPLATES = Path(reprintbuddy.web.__file__).parent / "templates"
CALL = re.compile(r"\{%\s*call\s+ui\.page_head\(")

# Pages that show a heading without `page_head`: the centred cards of sign-in and first-run setup.
NO_HEAD = {"login.html", "setup_admin.html", "setup_bambuddy_connect.html", "setup_bambuddy_key.html", "setup_bambuddy_user.html"}
# page_head calls allowed to have no subtitle, each with its reason (none today).
NO_SUBTITLE: dict[str, str] = {}


def _arguments(text, start):
    """The top-level arguments of the call whose `(` is just before `start`."""
    args, depth, quote, current = [], 1, None, ""
    for char in text[start:]:
        if quote:
            current += char
            quote = None if char == quote else quote
        elif char in "\"'":
            quote = char
            current += char
        elif char in "([{":
            depth += 1
            current += char
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                args.append(current.strip())
                return args
            current += char
        elif char == "," and depth == 1:
            args.append(current.strip())
            current = ""
        else:
            current += char
    raise AssertionError("unclosed page_head call")


def test_every_page_head_has_a_subtitle():
    missing, heads = [], 0
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in CALL.finditer(text):
            heads += 1
            args = _arguments(text, match.end())
            if len(args) < 3 or not args[2]:
                if path.name not in NO_SUBTITLE:
                    missing.append(path.name)
    assert heads > 25  # the scan found the calls
    assert missing == []


def test_pages_without_a_head_are_the_known_cards():
    without = [p.name for p in sorted(TEMPLATES.glob("*.html")) if p.name != "_ui.html" and "<h1" in p.read_text(encoding="utf-8") and not CALL.search(p.read_text(encoding="utf-8"))]
    assert set(without) == NO_HEAD

