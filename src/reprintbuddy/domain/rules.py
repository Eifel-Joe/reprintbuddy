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
    # A choice counts the option with the smallest stock (Spec 5.4, v0.3.16 p173): the set is as often
    # deliverable as it can be made with every one of the options, so they are not added up. One option
    # is the same case; a negative (oversold) option stays negative.
    return min(a // position.qty for a in options)


def set_available(positions: Sequence[PositionStock]) -> int:
    """Set availability: the minimum over its positions (Spec 5.4); 0 without positions.

    A choice position counts its smallest option (v0.3.16 p173). Positions that use the same article
    (fixed or as an option) need it at the same time when the customer picks it, so the worst choice
    counts: per article the quantities of all positions that hold it are added up before dividing (A once
    in two positions needs 2 A per set). For an article that is oversold the worst choice is the one that
    takes the least of it: it counts with the fixed quantity, or else the smallest quantity of a choice.
    A position without article ids (not known to the caller) counts on its own.

    This is exact, not an approximation (p321): the set counts its worst combination of choices, the
    minimum over all combinations of the minimum over the articles. Minima can be swapped, so it is the
    minimum over the articles of each article's own worst combination, and the positions are chosen
    independently, so each article reaches its own one, even when those of two articles exclude each
    other. Tests: tests/domain/test_rules.py (checked against every combination).
    """
    if not positions:
        return 0
    fixed: dict[int, int] = {}
    choices: dict[int, list[int]] = {}
    in_stock: dict[int, int] = {}
    values: list[int] = []
    for position in positions:
        if not position.option_available or len(position.article_ids) != len(position.option_available):
            values.append(_position_available(position))
            continue
        for article_id, stock in zip(position.article_ids, position.option_available):
            in_stock[article_id] = stock
            if len(position.option_available) == 1:
                fixed[article_id] = fixed.get(article_id, 0) + position.qty
            else:
                choices.setdefault(article_id, []).append(position.qty)
    for article_id, stock in in_stock.items():
        picked = choices.get(article_id, [])
        if stock >= 0:
            need = fixed.get(article_id, 0) + sum(picked)
        else:
            need = fixed.get(article_id) or min(picked)
        values.append(stock // need)
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
