"""v0.3.13 p143: release notes as simple Markdown, always escaped (Spec 11.3a "Neuerungen").

Paragraphs, lists, bold, italic and code; links and images only as their text; no HTML from the text.
"""

from __future__ import annotations

import re

import pytest
from markupsafe import Markup

from reprintbuddy.web.notes_markdown import render_notes

ALLOWED_TAGS = {"p", "ul", "ol", "li", "strong", "em", "code", "pre"}


def tags_in(html: str) -> set[str]:
    return {name.lower() for name in re.findall(r"</?([A-Za-z][A-Za-z0-9]*)", html)}


def test_the_result_is_markup():
    assert isinstance(render_notes("x"), Markup)


def test_paragraphs():
    assert render_notes("One\nline.\n\nTwo.") == "<p>One\nline.</p>\n<p>Two.</p>"


def test_lists():
    assert render_notes("Intro:\n- a\n- b\n\n1. one\n2. two") == (
        "<p>Intro:</p>\n<ul><li>a</li><li>b</li></ul>\n<ol><li>one</li><li>two</li></ol>"
    )


def test_star_and_plus_lists():
    assert render_notes("* a\n+ b") == "<ul><li>a</li><li>b</li></ul>"


def test_bold_italic_code():
    assert render_notes("**bold**, *it*, _it_, __bold__ and `a*b*c`") == (
        "<p><strong>bold</strong>, <em>it</em>, <em>it</em>, <strong>bold</strong> and <code>a*b*c</code></p>"
    )


def test_bold_and_italic_do_not_interleave():
    """Review: `*a **b* c**` gave <em>a <strong>b</em> c</strong>."""
    html = str(render_notes("*a **b* c**"))
    stack = []
    for closing, name in re.findall(r"<(/?)([a-z]+)>", html):
        if closing:
            assert stack and stack.pop() == name, html
        else:
            stack.append(name)
    assert stack == []


def test_bold_italic_together():
    assert render_notes("***x***") == "<p><strong><em>x</em></strong></p>"


def test_underscores_inside_words_stay():
    assert render_notes("snake_case_name") == "<p>snake_case_name</p>"


def test_headings_become_a_bold_line():
    assert render_notes("## What's new\nText") == "<p><strong>What&#39;s new</strong></p>\n<p>Text</p>"


def test_code_block():
    assert render_notes("```\n<b>x</b>\n  y\n```") == "<pre><code>&lt;b&gt;x&lt;/b&gt;\n  y</code></pre>"


def test_links_and_images_are_only_text():
    html = render_notes("See [the docs](https://example.com/x) and ![logo](https://example.com/l.png).")
    assert html == "<p>See the docs and logo.</p>"


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "[click](javascript:alert(1))",
        '[x](https://example.com/" onmouseover="alert(1))',
        "![x](https://tracker.example/p.gif)",
        "<https://example.com>",
        '<a href="https://example.com">x</a>',
        "**<i>x</i>**",
        "`</code><script>x</script>`",
        "- <iframe src=//example.com></iframe>",
        "```\n</pre><script>x</script>\n```",
        "&lt;script&gt; &amp; &#60;",
        "<style>body{display:none}</style>",
        '"><svg onload=alert(1)>',
    ],
)
def test_nothing_from_the_text_becomes_html(text):
    html = str(render_notes(text))
    assert tags_in(html) <= ALLOWED_TAGS
    assert not re.search(r"<[A-Za-z][A-Za-z0-9]*\s", html)  # no tag carries an attribute
    assert "<script" not in html and "<img" not in html and "<a " not in html and "<svg" not in html


def test_entities_in_the_text_are_shown_as_typed():
    assert render_notes("a &amp; b < c") == "<p>a &amp;amp; b &lt; c</p>"


def test_empty_text():
    assert render_notes("") == ""
    assert render_notes("  \n\n ") == ""


def test_cut_mark_is_not_part_of_the_renderer():
    assert "gekürzt" not in render_notes("x") and "cut" not in render_notes("x")
