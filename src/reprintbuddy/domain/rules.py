"""Pure calculation rules, no database and no web (B4 adds coverage and prints)."""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date


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


def prints_needed(*, min_stock: int, buffer: int, coverage: int, units_per_print: int) -> int:
    """Drucke = ceil((Mindestbestand + Puffer − Deckung) / Stück je Druck), 0 if covered (Spec 7.1).

    Without a minimum and without a buffer nothing is printed for the article (Spec 5.2).
    """
    if (min_stock == 0 and buffer == 0) or coverage >= min_stock:
        return 0
    return _ceil_div(min_stock + buffer - coverage, units_per_print)


def prints_for_units(units: int, units_per_print: int) -> int:
    """Drucke für eine Stückzahl = ceil(Stück / Stück je Druck), 0 for no units (Spec 7.5)."""
    if units <= 0:
        return 0
    return _ceil_div(units, units_per_print)


def _ceil_div(numerator: int, denominator: int) -> int:
    # Integer ceil without floats: -(-n // d) rounds towards +infinity for d > 0.
    return -(-numerator // denominator)


def consumption_per_day(
    series: Mapping[Hashable, Sequence[tuple[date, float]]],
) -> tuple[float, int]:
    """Verbrauch je Tag and the days of history (Spec 9.2).

    The caller passes only the points of the window. Per series only the decreases between
    consecutive points count (increases, new and vanished spools add nothing); the sum is
    divided by the days between the earliest and latest day over all series, at most 30.
    Under two distinct days: (0.0, 0).
    """
    days = {day for points in series.values() for day, _ in points}
    if len(days) < 2:
        return 0.0, 0
    span = min((max(days) - min(days)).days, 30)
    used = 0.0
    for points in series.values():
        ordered = sorted(points, key=lambda point: point[0])
        for (_, before), (_, after) in zip(ordered, ordered[1:]):
            if after < before:
                used += before - after
    return used / span, span


def safety_amount(value: float, unit: str, per_day: float) -> float:
    """Sicherheitspolster: unit "days" -> Verbrauch je Tag x value, else the value itself (Spec 9.3)."""
    if unit == "days":
        return per_day * value
    return float(value)


def reorder_floor(safety: float, min_stock: float) -> float:
    """Untergrenze eines Zukaufteils: der größere Wert aus Sicherheitspolster und Mindestbestand (Spec 9.3).

    The two do not add up. A filament sort has no minimum stock and uses its safety stock alone.
    """
    return max(safety, float(min_stock))


def needs_reorder(
    *, stock: float, demand: float, per_day: float, lead_days: int, safety: float
) -> bool:
    """Reorder when Bestand − Bedarf − Verbrauch × Lieferzeit < Polster (Spec 9.3)."""
    return stock - demand - per_day * lead_days < safety


def suggest_packs(
    *,
    stock: float,
    demand: float,
    per_day: float,
    lead_days: int,
    safety: float,
    pack: float,
    min_packs: int,
) -> int:
    """Bestelleinheiten = max(Mindestmenge, ceil(Fehlmenge / Bestelleinheit)) (Spec 9.3).

    The ceiling tolerates float residue of 1e-9; no shortfall gives the minimum order.
    """
    shortfall = safety + demand + per_day * lead_days - stock
    if shortfall <= 0:
        return min_packs
    return max(min_packs, math.ceil(shortfall / pack - 1e-9))
