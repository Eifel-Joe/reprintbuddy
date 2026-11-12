"""Number fields in the rows of the set form and the form of an article made of parts on a phone (p390).

With a finger the two arrows of a number field sit side by side and take 5.5rem (spinbox.js, Spec
10.1: every number field keeps its arrows). A quantity column of 4.5rem left the field less room than
its arrows: it reached into the drop-down beside it and the arrows covered the number. The hand check
at 375 px measures the room (v0.4.5 report); this test holds the widths it was measured with."""

import re
from pathlib import Path

CSS = (Path(__file__).parents[2] / "src" / "reprintbuddy" / "web" / "static" / "css" / "reprintbuddy.css").read_text(
    encoding="utf-8"
)
ARROWS_REM = 5.5


def _media_blocks(query: str) -> list[str]:
    """The bodies of every @media block with exactly this query (blocks hold one level of rules)."""
    return re.findall(r"@media " + re.escape(query) + r" \{((?:[^{}]*\{[^}]*\})*)\s*\}", CSS)


def _columns(block: str, selector: str) -> str:
    found = re.findall(r"(?:^|\n)\s*" + re.escape(selector) + r" \{[^}]*grid-template-columns: ([^;]+);", block)
    assert found, selector
    return found[-1]


def _first_rem(columns: str) -> float:
    return float(re.match(r"([\d.]+)rem", columns).group(1))


PHONE_FINGER = "(pointer: coarse) and (max-width: 480px)"


def test_quantity_column_has_room_for_the_arrows_and_a_four_digit_number():
    blocks = _media_blocks(PHONE_FINGER)
    assert blocks, PHONE_FINGER
    width = _first_rem(_columns(blocks[-1], ".set-base"))
    assert width - ARROWS_REM >= 3.5, width  # "1000", the padding and a margin for wider fonts beside the arrows


def test_variant_table_takes_two_lines_on_a_phone():
    columns = _columns(_media_blocks(PHONE_FINGER)[-1], ".assembled-variant")
    assert columns == "repeat(2, minmax(0, 1fr))"  # number and stock, then minimum and buffer
