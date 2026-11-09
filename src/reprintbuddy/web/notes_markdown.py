"""Release notes as simple Markdown, always escaped (v0.3.13 p143, Spec 11.3a "Neuerungen").

The notes come from GitHub, so they are text from outside. The whole text is escaped first; only
then do fixed tags without attributes go in: paragraphs, lists, bold, italic, code and code blocks.
A heading becomes a bold line. Links and images keep only their text (the address is dropped), raw
HTML stays visible text. No tag ever carries an attribute, so no link, no script, no style and no
picture from the text reaches the page. Rejected: GitHub's `body_html` (its cleaning is not
documented) and a Markdown library plus an HTML cleaner (two new dependencies for a few rules).
Tests: tests/web/test_notes_markdown.py.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d{1,9}[.)]\s+(.*)$")

# Applied to text that is already escaped: `[`, `]`, `(`, `)`, `*`, `_` and backticks survive escaping.
_CODE = re.compile(r"`([^`]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_EM = re.compile(r"\*(?=\S)(.+?)(?<=\S)\*|(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_SLOT = "\x00{}\x00"  # stands for a code span while the other rules run; NUL never comes from escaping


def _inline(text: str) -> str:
    """One line or paragraph: escaped, then the fixed inline tags."""
    html = str(escape(text))
    codes: list[str] = []

    def keep_code(found: re.Match[str]) -> str:
        codes.append(f"<code>{found[1]}</code>")
        return _SLOT.format(len(codes) - 1)

    html = html.replace("\x00", "")
    html = _CODE.sub(keep_code, html)
    html = _IMAGE.sub(lambda m: m[1], html)
    html = _LINK.sub(lambda m: m[1], html)
    html = _STRONG.sub(lambda m: f"<strong>{m[1] or m[2]}</strong>", html)
    html = _EM.sub(lambda m: f"<em>{m[1] or m[2]}</em>", html)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m[1])], html)


def render_notes(text: str) -> Markup:
    """The HTML of one version's notes."""
    blocks: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    list_tag = ""
    code: list[str] | None = None

    def end_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{_inline(chr(10).join(paragraph))}</p>")
            paragraph.clear()

    def end_list() -> None:
        nonlocal list_tag
        if items:
            blocks.append(f"<{list_tag}>" + "".join(f"<li>{item}</li>" for item in items) + f"</{list_tag}>")
            items.clear()
        list_tag = ""

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if code is not None:
            if _FENCE.match(line):
                blocks.append("<pre><code>" + str(escape("\n".join(code))) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if _FENCE.match(line):
            end_paragraph()
            end_list()
            code = []
            continue
        if not line.strip():
            end_paragraph()
            end_list()
            continue
        heading = _HEADING.match(line)
        bullet = _BULLET.match(line)
        numbered = _NUMBERED.match(line)
        if heading:
            end_paragraph()
            end_list()
            blocks.append(f"<p><strong>{_inline(heading[1])}</strong></p>")
        elif bullet or numbered:
            end_paragraph()
            wanted = "ul" if bullet else "ol"
            if list_tag and list_tag != wanted:
                end_list()
            list_tag = wanted
            items.append(_inline((bullet or numbered)[1]))
        elif items:  # a continued list item
            items[-1] += "\n" + _inline(line.strip())
        else:
            paragraph.append(line.strip())
    if code is not None:  # an open fence: what follows is still shown, escaped
        blocks.append("<pre><code>" + str(escape("\n".join(code))) + "</code></pre>")
    end_paragraph()
    end_list()
    return Markup("\n".join(blocks))
