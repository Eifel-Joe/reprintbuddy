"""Pure calculation rules, no database and no web (B4 adds coverage and prints)."""

from __future__ import annotations

import itertools
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
class ChoiceStock:
    """One choice field of a set (Spec 5.4): the quantity of the chosen article, and per option the
    article id and its available count, in the order of the form."""

    qty: int
    options: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class SetStock:
    """`total` is "zusammen hoechstens", `variants` the count per variant: the key holds the chosen
    article ids, one per field, in the order of the form; empty for a set without choices."""

    total: int
    variants: dict[tuple[int, ...], int]


def set_availability(base: Sequence[tuple[int, int]], choices: Sequence[ChoiceStock]) -> SetStock:
    """Availability of a set from (qty, available) per base part and its choice fields (Spec 5.4, p340).

    A variant is as often available as a set of its own articles: the minimum over the base parts and
    its chosen articles. Together, in any mix of the variants, the base parts limit the count, and each
    field gives the sum of what its articles give, because the customer picks one per set: an article
    of a field never takes stock of another one (an article is only once in a set, Spec 5.4). An
    oversold option (negative) adds nothing to the sum; when no option gives anything the field counts
    its best option, so a field of one article counts like a base part.
    Tests: tests/domain/test_rules.py (checked against the most sets of every mix).
    """
    limits = [available // qty for qty, available in base]
    if any(not field.options for field in choices) or (not limits and not choices):
        return SetStock(0, {})
    per_field = [[(article_id, available // field.qty) for article_id, available in field.options] for field in choices]
    totals = list(limits)
    for values in per_field:
        positive = sum(v for _, v in values if v > 0)
        totals.append(positive if positive > 0 else max(v for _, v in values))
    variants: dict[tuple[int, ...], int] = {}
    if per_field:
        for combination in itertools.product(*per_field):
            variants[tuple(a for a, _ in combination)] = min(limits + [v for _, v in combination])
    return SetStock(min(totals), variants)


def variant_number(number: str | None, suffixes: Sequence[str | None]) -> str | None:
    """The article number of a variant: the base number with the suffixes behind it, written as they
    are, without a separator (Spec 5.4, p340). None while the base number or a suffix is missing."""
    if not number or any(not suffix for suffix in suffixes):
        return None
    return number + "".join(str(suffix) for suffix in suffixes)


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
