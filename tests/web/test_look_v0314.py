"""Look rules of v0.3.14, checked in the stylesheet text and in the page; the measured result at
1280 and 375 px is in the notes (v0314a-*.jpg)."""

import re

CSS = "/static/css/reprintbuddy.css"


def rule(css, selector):
    """The bodies of all rules whose selector list is exactly `selector`, joined (later rules refine earlier)."""
    bodies = re.findall(rf"(?:^|\}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", css, re.M)
    assert bodies, selector
    return "\n".join(bodies)


def test_key_table_heads_may_wrap(ready_client):  # p187
    """Every table head is one line (`th { white-space: nowrap }`). In the table of the rights of the key
    the head also holds the sentence what the right is for: left on one line it pushed the table, with the
    column "Reprintbuddy", out of its card. Measured before: table right edge 1008 px, card 900 px at 1280 px."""
    css = ready_client.get(CSS).text
    assert "white-space: normal" in rule(css, "table.keytable th")
    assert "overflow-wrap: anywhere" in rule(css, "table.keytable th")
