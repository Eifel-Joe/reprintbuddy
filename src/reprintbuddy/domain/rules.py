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
    article_ids: tuple[int, ...] = ()  # the same order as option_available


def _position_available(position: PositionStock) -> int:
    options = position.option_available
    if not options:
        return 0  # all articles archived (Spec 5.5)
    if len(options) == 1:
        return options[0] // position.qty  # may stay negative
    # A choice: every order picks one of the articles, so the options add up.
    return sum(max(0, a // position.qty) for a in options)


def set_available(positions: Sequence[PositionStock]) -> int:
    """Set availability: the minimum over its positions (Spec 5.4); 0 without positions.

    Fixed positions that use the same article need it at the same time, so their quantities
    are added up before dividing (A once in two positions needs 2 A per set).
    """
    if not positions:
        return 0
    needed: dict[int, int] = {}
    in_stock: dict[int, int] = {}
    values: list[int] = []
    for position in positions:
        if len(position.option_available) == 1 and len(position.article_ids) == 1:
            article_id = position.article_ids[0]
            needed[article_id] = needed.get(article_id, 0) + position.qty
            in_stock[article_id] = position.option_available[0]
        else:
            values.append(_position_available(position))
    values += [in_stock[a] // needed[a] for a in needed]
    return min(values)


def order_ready(needed: Mapping[int, int], stock: Mapping[int, int]) -> bool:
    """An order can ship when every article is in stock in the needed quantity (Spec 6.7)."""
    return all(stock.get(article_id, 0) >= qty for article_id, qty in needed.items())


def coverage(
    available: int,
    queued_units: int,
    in_progress_units: int,
    provisional: int,
    awaiting_assembly: int,
) -> int:
    """Deckung = Verfügbar + eingereiht + in Arbeit + vorläufig + wartet auf Montage (Spec 7.1)."""
    return available + queued_units + in_progress_units + provisional + awaiting_assembly
