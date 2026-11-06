"""Status rules: pure functions that map existing numbers to a state and a tone (Spec 10.1).

They only show what is already calculated; no new business logic lives here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

ArticleStatus = Literal["overbooked", "below_min", "low", "ok"]
PartStatus = Literal["below_min", "ok"]
SetStatus = Literal["deliverable", "not_deliverable"]
OrderStatus = Literal["open", "ready", "shipped", "cancelled"]
Tone = Literal["ok", "warn", "bad", "neutral"]

# The order is the one used for the short summary and for filters.
STATES: dict[str, tuple[str, ...]] = {
    "article": ("overbooked", "below_min", "low", "ok"),
    "part": ("below_min", "ok"),
    "set": ("not_deliverable", "deliverable"),
    "order": ("open", "ready", "shipped", "cancelled"),
}

_TONES: dict[str, Tone] = {
    "overbooked": "bad",
    "below_min": "bad",
    "not_deliverable": "bad",
    "low": "warn",
    "open": "warn",
    "ok": "ok",
    "deliverable": "ok",
    "ready": "ok",
    "shipped": "neutral",
    "cancelled": "neutral",
    "paused": "neutral",
    "archived": "neutral",
}


def article_status(*, available: int, min_stock: int, coverage: int) -> ArticleStatus:
    """The first row that applies wins."""
    if available < 0:
        return "overbooked"
    if available < min_stock and coverage < min_stock:
        return "below_min"
    if available < min_stock:
        return "low"
    return "ok"


def part_status(*, stock: int, min_stock: int) -> PartStatus:
    return "below_min" if stock < min_stock else "ok"


def set_status(*, available: int) -> SetStatus:
    return "deliverable" if available >= 1 else "not_deliverable"


def order_status(*, status: str, ready: bool) -> OrderStatus:
    if status == "open" and ready:
        return "ready"
    return status  # type: ignore[return-value]  # open, shipped or cancelled


def tone(state: str) -> Tone:
    try:
        return _TONES[state]
    except KeyError:
        raise ValueError(f"unknown state: {state!r}") from None


def summarize(states: Iterable[str], shown: Sequence[str]) -> list[tuple[str, int]]:
    """Count per state from `shown`, in its order, only counts above zero."""
    counts: dict[str, int] = {}
    for state in states:
        counts[state] = counts.get(state, 0) + 1
    return [(state, counts[state]) for state in shown if counts.get(state, 0) > 0]
