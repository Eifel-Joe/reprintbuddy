"""The numbers of the overview (Spec 10.2): the state of the Bambuddy connection and the cards of what is due.

Nothing is computed twice: the figures count the rows of `catalog.list_articles` (status after
the rules of Spec 10.1, coverage after 7.1), the prints of `workqueue` and the suggestions of
`reorder.count_suggestions`, the same functions the article list, the queue and the reorder page use.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from reprintbuddy.domain.engine import EngineStatus
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.kpis import Kpi

# "Below minimum stock" means the red states, as Spec 10.6 (event 4) uses the term: "low" (yellow)
# is below the minimum too, but its reprint is already planned.
RED_STATES = ("below_min", "overbooked")

# The text of each connection state (written out, so the language files see every key in use).
CONNECTION_KEYS = {
    "connected": "overview.connection.connected",
    "unreachable": "overview.connection.unreachable",
    "rejected": "overview.connection.rejected",
    "not_configured": "overview.connection.not_configured",
    "unknown": "overview.connection.unknown",
}


class _Row(Protocol):  # the fields of catalog.ArticleRow the figures read
    status: str
    provisional: int
    awaiting_assembly: int


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


def table_rows(rows: Sequence[_Row]) -> list[_Row]:
    """The rows of the overview table (Spec 10.2, 0.4.0 p341): an article made of parts built on stock
    gives one row per variant (in progress and to check stay empty), one built on order none, as sets."""
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


def figures(
    lang: str, rows: Sequence[_Row], *, queued_prints: int, open_prints: int, suggestions: int
) -> list[Kpi]:
    """The four key figures of Spec 10.2, always all four (a 0 is the news here).

    `queued_prints`: prints in Reprintbuddy's queue, held entries included (they still count to
    the coverage); `open_prints`: prints handed over to Bambuddy and not finished.
    """

    def t(key: str, **params: object) -> str:
        return translate(lang, key, **params)

    red = sum(1 for r in rows if r.status in RED_STATES)
    overbooked = sum(1 for r in rows if r.status == "overbooked")
    planned = queued_prints + open_prints
    check = sum(r.provisional for r in rows)
    assembly = sum(r.awaiting_assembly for r in rows)
    return [
        Kpi(
            icon="triangle-alert", tone="bad" if red else "ok", value=red, label=t("overview.kpi.below_min"),
            note=t("overview.kpi.overbooked_note", n=overbooked) if overbooked else "", href="/articles",
        ),
        Kpi(
            icon="printer", tone="accent", value=planned, label=t("overview.kpi.planned"),
            note=t("overview.kpi.planned_note", queued=queued_prints, open=open_prints) if planned else "",
            href="/queue",
        ),
        Kpi(
            icon="clipboard-check", tone="warn" if check + assembly else "neutral", value=check + assembly,
            label=t("overview.kpi.to_check"),
            note=t("overview.kpi.to_check_note", check=check, assembly=assembly) if check + assembly else "",
            href="/assembly" if assembly and not check else "/inspection",  # where something waits
        ),
        Kpi(
            icon="shopping-bag", tone="warn" if suggestions else "neutral", value=suggestions,
            label=t("overview.kpi.reorder"), href="/reorder",
        ),
    ]


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
    """(below minimum, overbooked) over the rows, the red states of Spec 10.1 ("low" is yellow)."""
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
