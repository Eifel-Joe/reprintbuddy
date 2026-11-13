"""What the set form and the form of an article made of parts read the same way (0.4.7, R7).

Both forms send how many rows a part has and one article and quantity per row. Each had its own copy,
and p391 was fixed in one of them only: a count above the limit fell back to the default and dropped
every row past it without a message. Tests: tests/web/test_form_rows.py, and for the set form
tests/web/test_sets_pages.py::test_set_rows_above_the_limit_are_capped_not_dropped.
"""

from __future__ import annotations

from reprintbuddy.web.forms import MAX_ID, bounded_int, parse_int

MAX_ROW_QTY = 1000  # how many of one article a row may ask for


def row_count(form, name: str, default: int, maximum: int) -> int:
    """How many rows or options the form sent: `default` without a usable count, at most `maximum`."""
    value = bounded_int(str(form.get(name, "")).strip())
    if value is None or value < 1:
        return default
    return min(value, maximum)


def article_choice(raw: str, field: str) -> int:
    """The article id a row's drop-down sent."""
    return parse_int(raw, field=field, min=1, max=MAX_ID)


def qty(raw: str, field: str) -> int:
    """The quantity of a row."""
    return parse_int(raw, field=field, min=1, max=MAX_ROW_QTY)
