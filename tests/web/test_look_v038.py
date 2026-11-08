"""Look rules of v0.3.8 (part B), checked in the stylesheet text; the measured result at 1280 and
375 px is in the notes (v038b-*.jpg)."""

import re

CSS = "/static/css/reprintbuddy.css"


def rule(css, selector):
    """The bodies of all rules whose selector list is exactly `selector`, joined (later rules refine earlier)."""
    bodies = re.findall(rf"(?:^|\}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", css, re.M)
    assert bodies, selector
    return "\n".join(bodies)


def test_main_button_is_full_width_on_a_phone(ready_client):  # p71
    css = ready_client.get(CSS).text
    block = re.search(r"@media \(max-width: 375px\) \{(.*?)\n\}", css, re.S)
    assert block, "no rule for 375 px"
    assert ".page-actions > .btn:not(.secondary) { flex: 1 1 100%; }" in block.group(1)
    assert ".page-actions { flex: 1 1 100%; margin-left: 0; }" in block.group(1)


def test_english_key_figure_labels_are_not_hyphenated(ready_client):  # p112
    css = ready_client.get(CSS).text
    assert "hyphens: manual" in rule(css, ".kpi-label")
    assert "hyphens: auto" in rule(css, 'html[lang="de"] .kpi-label')
