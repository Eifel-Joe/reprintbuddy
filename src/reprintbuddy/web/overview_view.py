"""The numbers of the overview (Spec 10.2): the state of the Bambuddy connection and the cards of what is due.

Nothing is computed twice: the route counts with the functions of the pages the cards lead to (orders,
inspection, assembly, reorder, queue, filament, articles); this module only turns the counts into cards.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

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


class _Row(Protocol):  # the field of catalog.ArticleRow the count reads
    status: str


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


def status_rows(rows: Sequence[_Row]) -> list[_Row]:
    """The rows whose state the card "below minimum stock" counts (Spec 10.2, 0.4.0 p341): an article
    made of parts built on stock gives one row per variant, one built on order none, as sets."""
    from dataclasses import replace

    result: list[_Row] = []
    for row in rows:
        assembled = getattr(row, "assembled", None)
        if assembled is None:
            result.append(row)
            continue
        if assembled.mode != "stock":
            continue
        for holder in assembled.holders:
            result.append(replace(
                row, sku=holder.sku or row.sku, stock=holder.stock, reserved=holder.reserved,
                available=holder.available, min_stock=holder.min_stock, target=holder.target,
                buffer=holder.buffer, coverage=holder.available, status=holder.status,
            ))
    return result


# ---- "Was ansteht" (Spec 10.2 since 0.4.1) ------------------------------------------------------


@dataclass(frozen=True)
class Todo:
    """What is due, counted by the route with the functions of the pages the cards lead to.

    `filament_short` is None while the spool stock is not known (the card says so instead of a 0).
    """

    orders_open: int  # open orders, ready ones included (Spec 6.7)
    orders_new: int  # of them created since /orders was last loaded
    orders_ready: int
    checks: int  # prints waiting for the check
    check_units: int  # their pieces
    assembly_units: int  # pieces waiting for assembly + pieces of open orders built on order
    suggestions: int  # reorder suggestions not marked as ordered (Spec 9.4)
    suggestions_new: int  # of them not shown when /reorder was last loaded
    under_way: int  # marked as ordered, not arrived, not taken back
    printing: int  # prints Bambuddy runs (its entry is `printing`)
    waiting: int  # Reprintbuddy's queue (held entries too) + handed over, not started
    filament_short: int | None  # spool sorts with a reorder suggestion
    below_min: int  # rows (variants one by one) below the minimum
    overbooked: int


@dataclass(frozen=True)
class NotePart:
    text: str
    href: str = ""  # a link of its own inside the card (Spec 10.2: "versandbereit", "überbucht")


@dataclass(frozen=True)
class TodoCard:
    """One card: the whole card leads to `href`; `note` is the small line, its parts joined by " · "."""

    icon: str
    tone: str  # warn when something is due, bad for below the minimum, neutral at 0
    value: int | str  # a text when the number is not known
    label: str
    href: str
    note: tuple[NotePart, ...] = ()


def red_counts(rows: Sequence[_Row]) -> tuple[int, int]:
    """(below minimum, overbooked) over the rows, the red states of Spec 10.1, as Spec 10.6 (event 4)
    uses the term: "low" (yellow) is below the minimum too, but its reprint is already planned."""
    return (
        sum(1 for r in rows if r.status == "below_min"),
        sum(1 for r in rows if r.status == "overbooked"),
    )


def todo_cards(lang: str, todo: Todo) -> list[TodoCard]:
    """The seven cards of Spec 10.2 in its order, always all seven; a 0 is grey."""

    def t(key: str, **params: object) -> str:
        return translate(lang, key, **params)

    def parts(*pairs: tuple[int, str, str]) -> tuple[NotePart, ...]:
        return tuple(NotePart(text, href) for n, text, href in pairs if n > 0)

    def tone(n: int, due: str = "warn") -> str:
        return due if n > 0 else "neutral"

    red = todo.below_min + todo.overbooked
    filament: int | str = t("overview.todo.stock_unknown") if todo.filament_short is None else todo.filament_short
    return [
        TodoCard(
            "receipt", tone(todo.orders_open), todo.orders_open, t("overview.todo.orders"), "/orders",
            parts(
                (todo.orders_new, t("overview.todo.new", n=todo.orders_new), ""),
                (todo.orders_ready, t("overview.todo.orders_ready", n=todo.orders_ready), "/orders?status=ready"),
            ),
        ),
        TodoCard(
            "clipboard-check", tone(todo.checks), todo.checks, t("overview.todo.inspection"), "/inspection",
            parts((todo.check_units, t("overview.todo.pieces", n=todo.check_units), "")),
        ),
        TodoCard("wrench", tone(todo.assembly_units), todo.assembly_units, t("overview.todo.assembly"), "/assembly"),
        TodoCard(
            "shopping-bag", tone(todo.suggestions), todo.suggestions, t("overview.todo.reorder"), "/reorder",
            parts(
                (todo.suggestions_new, t("overview.todo.new", n=todo.suggestions_new), ""),
                (todo.under_way, t("overview.todo.under_way", n=todo.under_way), ""),
            ),
        ),
        TodoCard(
            "printer", tone(todo.printing), todo.printing, t("overview.todo.prints"), "/queue",
            parts((todo.waiting, t("overview.todo.waiting", n=todo.waiting), "")),
        ),
        TodoCard(
            "disc-3", tone(todo.filament_short or 0), filament, t("overview.todo.filament"), "/filament",
        ),
        TodoCard(
            "triangle-alert", tone(red, "bad"), red, t("overview.todo.below_min"), "/articles?status=below_min",
            parts((todo.overbooked, t("overview.todo.overbooked", n=todo.overbooked), "/articles?status=overbooked")),
        ),
    ]


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
