"""Pure calculation rules, no database and no web (B4 adds coverage and prints)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


def available(stock: int, reserved: int) -> int:
    """Verfügbar = Bestand − Reserviert; may be negative (Spec 6.4)."""
    return stock - reserved


def target_quantity(min_stock: int, buffer: int) -> int:
    """Zielmenge = Mindestbestand + Puffer (Spec 4)."""
    return min_stock + buffer


@dataclass(frozen=True)
class PositionStock:
    """One set position: how many of the article it needs, and the available count of each option."""

    qty: int
    option_available: tuple[int, ...]


def _position_available(position: PositionStock) -> int:
    options = position.option_available
    if not options:
        return 0  # all articles archived (Spec 5.5)
    if len(options) == 1:
        return options[0] // position.qty  # may stay negative
    # A choice: every order picks one of the articles, so the options add up.
    return sum(max(0, a // position.qty) for a in options)


def set_available(positions: Sequence[PositionStock]) -> int:
    """Set availability: the minimum over its positions (Spec 5.4); 0 without positions."""
    if not positions:
        return 0
    return min(_position_available(p) for p in positions)


def order_ready(needed: Mapping[int, int], stock: Mapping[int, int]) -> bool:
    """An order can ship when every article is in stock in the needed quantity (Spec 6.7)."""
    return all(stock.get(article_id, 0) >= qty for article_id, qty in needed.items())
