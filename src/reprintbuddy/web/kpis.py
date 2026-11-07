"""Cards of the key figures above a list (Spec 10.1): the total always, then one card for each
state that has entries. The cards only show numbers the page already has."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from reprintbuddy.domain import status

# Symbol per state (the colour comes from the tone of the state).
_ICONS = {
    "overbooked": "circle-alert",
    "below_min": "triangle-alert",
    "not_deliverable": "triangle-alert",
    "low": "circle-alert",
    "open": "circle-alert",
    "ready": "check",
    "shipped": "check",
    "cancelled": "x",
    "paused": "pause",
}


@dataclass(frozen=True)
class Kpi:
    """One card. `tone`: accent, ok, warn, bad, neutral or info; `label` is the finished text."""

    icon: str
    tone: str
    value: int
    label: str


def state_cards(
    t: Callable[[str], str],
    kind: str,
    icon: str,
    total: int,
    total_label: str,
    counts: Sequence[tuple[str, int]],
) -> list[Kpi]:
    """The total card, then one card per `(state, n)` with `n > 0`, in the given order.

    `t` translates a key; the labels are the ones of the badges (`status.<kind>.<state>`,
    `status.paused`).
    """
    cards = [Kpi(icon=icon, tone="accent", value=total, label=total_label)]
    for state, n in counts:
        if n <= 0:
            continue
        key = "status.paused" if state == "paused" else f"status.{kind}.{state}"
        cards.append(Kpi(icon=_ICONS.get(state, "circle-alert"), tone=status.tone(state), value=n, label=t(key)))
    return cards


def filament_cards(
    t: Callable[[str], str], sorts: int, stock_g: int | None, spools: int | None, unconfigured: int
) -> list[Kpi]:
    """Cards of the filament page: the sorts, the total stock in grams, the spools.

    Stock and spools are `None` while the stock is not known (Bambuddy cannot be reached and
    nothing is recorded): they are left out then, never shown as 0. `unconfigured` counts the
    sorts without settings, which get no order suggestions (Spec 9.3); its card shows only
    above zero.
    """
    cards = [Kpi(icon="disc-3", tone="accent", value=sorts, label=t("kpi.filament_sorts"))]
    if stock_g is not None:
        cards.append(Kpi(icon="boxes", tone="info", value=stock_g, label=t("kpi.filament_stock")))
    if spools is not None:
        cards.append(Kpi(icon="package", tone="neutral", value=spools, label=t("kpi.filament_spools")))
    if unconfigured > 0:
        cards.append(Kpi(
            icon="triangle-alert", tone="warn", value=unconfigured, label=t("kpi.filament_unconfigured"),
        ))
    return cards


def reorder_cards(t: Callable[[str], str], filament: int, parts: int, under_way: int) -> list[Kpi]:
    """Cards of the reorder page: the open suggestions (yellow, Spec 10.1), split by kind, and what
    is on its way. Like the state cards, a card besides the total shows only above zero."""
    total = filament + parts
    cards = [Kpi(
        icon="shopping-bag", tone="warn" if total else "accent", value=total, label=t("kpi.reorder_suggestions"),
    )]
    if filament > 0:
        cards.append(Kpi(icon="disc-3", tone="warn", value=filament, label=t("nav.filament")))
    if parts > 0:
        cards.append(Kpi(icon="puzzle", tone="warn", value=parts, label=t("kpi.parts")))
    if under_way > 0:
        cards.append(Kpi(icon="clock", tone="info", value=under_way, label=t("reorder.under_way")))
    return cards
