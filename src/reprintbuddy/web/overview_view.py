"""The numbers of the overview (Spec 10.2): the state of the Bambuddy connection and the four tiles.

Nothing is computed twice: the route counts with the functions of the pages the tiles lead to (orders,
inspection, assembly, reorder, filament, articles); this module only turns the counts into tiles.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from reprintbuddy.domain.engine import EngineStatus
from reprintbuddy.domain.reorder import Reach
from reprintbuddy.i18n import translate_count
from reprintbuddy.web.i18n import translate

# The text of each connection state (written out, so the language files see every key in use).
CONNECTION_KEYS = {
    "connected": "overview.connection.connected",
    "unreachable": "overview.connection.unreachable",
    "rejected": "overview.connection.rejected",
    "not_configured": "overview.connection.not_configured",
    "unknown": "overview.connection.unknown",
}


def connection_state(status: EngineStatus) -> tuple[str, str]:
    """(state, tone) of the connection to Bambuddy, from the same fields as the warnings.

    Not answering is checked before a refused query: the engine keeps an old refusal while
    Bambuddy cannot be reached, and "not reachable" is the newer fact then.
    """
    if not status.configured:
        return "not_configured", "neutral"
    if status.reachable is False:
        return "unreachable", "bad"
    if status.poll_error:
        return "rejected", "bad"
    if status.reachable is None:  # set up, but no regular query has run since the start
        return "unknown", "neutral"
    return "connected", "ok"


# ---- the four tiles (Spec 10.2 since 0.4.2) -----------------------------------------------------

# The states of the stock tile, in the order of its bar: the article states of Spec 10.1 from good to bad.
# An article made of parts built on order is deliverable or not, like a set: it has no stock to count.
STOCK_STATES = ("ok", "low", "below_min", "overbooked")
_STOCK_TONES = {"ok": "ok", "low": "warn", "below_min": "bad", "overbooked": "bad"}
_STOCK_TEXTS = {
    "ok": "overview.tile.stock_ok",
    "low": "overview.tile.stock_low",
    "below_min": "overview.tile.stock_below_min",
    "overbooked": "overview.tile.stock_overbooked",
}
FILAMENT_ROWS = 3  # the spool sorts with the shortest reach (Spec 10.2)


@dataclass(frozen=True)
class Part:
    """A part of a tile's bar and of its line in words; `href` leads to the filter of exactly what it counts."""

    key: str
    count: int
    text: str
    href: str = ""
    tone: str = "neutral"


@dataclass(frozen=True)
class OrdersTile:
    count: int
    tone: str
    parts: tuple[Part, ...]  # ready to ship, waiting for stock (only those there are)
    new: str  # "n new", or "" for none


@dataclass(frozen=True)
class StockTile:
    total: int
    title: str
    parts: tuple[Part, ...]


@dataclass(frozen=True)
class FilamentRow:
    label: str
    text: str
    tone: str


@dataclass(frozen=True)
class FilamentTile:
    count: int | str  # a text while the spool stock is not known
    tone: str
    rows: tuple[FilamentRow, ...]
    young: bool  # a row rests on less than 30 days of usage
    young_text: str


@dataclass(frozen=True)
class TodoLine:
    label: str
    count: int
    href: str
    tone: str
    note: str


def _due(n: int) -> str:
    return "warn" if n > 0 else "neutral"


def orders_tile(lang: str, *, open_count: int, ready: int, new: int) -> OrdersTile:
    """Open orders (open + ready, Spec 6.7): ready to ship (green) and waiting for stock (status open)."""
    waiting = open_count - ready
    candidates = (
        Part("ready", ready, translate_count(lang, "overview.tile.orders_ready", ready), "/orders?status=ready", "ok"),
        Part("waiting", waiting, translate_count(lang, "overview.tile.orders_waiting", waiting),
             "/orders?status=open", "warn"),
    )
    new_text = translate(lang, "overview.tile.new", n=new) if new else ""
    return OrdersTile(open_count, _due(open_count), tuple(p for p in candidates if p.count > 0), new_text)


def stock_tile(lang: str, statuses: Sequence[str]) -> StockTile:
    """One status per row of the articles page (`catalog.list_articles`): an article made of parts on stock
    with the worst state of its variants. So each part's filter shows as many rows as it counts (p420), and
    "low" - its reprint is running - is shown too (p421)."""
    counted = [s for s in statuses if s in STOCK_STATES]
    parts = tuple(
        Part(state, n, translate_count(lang, _STOCK_TEXTS[state], n), f"/articles?status={state}", _STOCK_TONES[state])
        for state in STOCK_STATES
        if (n := counted.count(state)) > 0
    )
    return StockTile(len(counted), translate_count(lang, "overview.tile.stock", len(counted)), parts)


def filament_tile(lang: str, *, short: int | None, reach: Sequence[Reach]) -> FilamentTile:
    """Spool sorts that are low (a suggestion not marked as ordered, Spec 9.4) and the shortest reaches.
    `short` None: the spool stock is not known."""
    if short is None:
        return FilamentTile(translate(lang, "overview.tile.stock_unknown"), "neutral", (), False, "")
    shown = tuple(reach[:FILAMENT_ROWS])
    rows = tuple(
        FilamentRow(
            r.label,
            translate(lang, "overview.tile.no_usage") if r.days is None
            else translate_count(lang, "overview.tile.lasts", r.days),
            "warn" if r.short else "ok",
        )
        for r in shown
    )
    return FilamentTile(short, _due(short), rows, any(r.young for r in shown), translate(lang, "overview.tile.young"))


def todo_lines(
    lang: str, *, checks: int, check_units: int, assembly_units: int, suggestions: int, suggestions_new: int,
    under_way: int,
) -> tuple[TodoLine, ...]:
    """To check, assembly, reorder suggestions: always all three, each leading to its page (Spec 10.2)."""
    reorder_note = " · ".join(
        text for n, text in (
            (suggestions_new, translate(lang, "overview.tile.new", n=suggestions_new)),
            (under_way, translate(lang, "overview.tile.under_way", n=under_way)),
        ) if n > 0
    )
    pieces = translate_count(lang, "overview.tile.pieces", check_units) if check_units else ""
    return (
        TodoLine(translate(lang, "overview.tile.inspection"), checks, "/inspection", _due(checks), pieces),
        TodoLine(translate(lang, "overview.tile.assembly"), assembly_units, "/assembly", _due(assembly_units), ""),
        TodoLine(translate(lang, "overview.tile.reorder"), suggestions, "/reorder", _due(suggestions), reorder_note),
    )
