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
        cards.append(Kpi(icon=_ICONS[state], tone=status.tone(state), value=n, label=t(key)))
    return cards
