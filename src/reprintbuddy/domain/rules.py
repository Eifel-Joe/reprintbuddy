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
    article ids (sets) or option ids (articles made of parts), one per field, in the order of the form;
    empty for a set without choices. `capped` is true when the search for "zusammen hoechstens" stopped
    at MAX_NODES, so `total` is a lower bound (0.4.0, p341)."""

    total: int
    variants: dict[tuple[int, ...], int]
    capped: bool = False


@dataclass(frozen=True)
class Bundle:
    """One option of a choice field (0.4.0, p341): its key (sets: the article id, articles made of parts:
    the option id) and what one piece takes, as (article_id, quantity) pairs."""

    key: int
    needs: tuple[tuple[int, int], ...]


# Nodes the exact search of one field may visit; past it the best mix found so far counts (a lower bound).
MAX_NODES = 200_000


def set_availability(base: Sequence[tuple[int, int]], choices: Sequence[ChoiceStock]) -> SetStock:
    """Availability of a set from (qty, available) per base part and its choice fields (Spec 5.4, p340).

    Since 0.4.0 an adapter over bundle_availability: each base part gets a stand-in id below zero (no
    article id is negative), each option is a bundle of one article in the quantity of its field.
    Tests: tests/domain/test_rules.py (Part A's set tests stay unchanged).
    """
    stock: dict[int, int] = {}
    base_needs = []
    for index, (qty, avail) in enumerate(base):
        stock[-(index + 1)] = avail
        base_needs.append((-(index + 1), qty))
    fields = []
    for field in choices:
        bundles = []
        for article_id, avail in field.options:
            stock[article_id] = avail
            bundles.append(Bundle(article_id, ((article_id, field.qty),)))
        fields.append(bundles)
    return bundle_availability(base_needs, fields, stock)


def _bundle_value(bundle: Bundle, left: Mapping[int, int]) -> int:
    return min(left[a] // q for a, q in bundle.needs)


def _field_most(bundles: Sequence[Bundle], stock: Mapping[int, int]) -> tuple[int, bool]:
    """The most pieces one field gives in any mix of its options, and whether the search was capped.

    Root: with articles made of parts an article may sit in several options of a field (the hose's side
    part L in -L32 and -L40), so the sum of the options counts it twice. Exact means: the largest
    sum of y_b with sum_b need(a, b) * y_b <= stock[a] for every article, an integer packing problem.
    Fields are small (a few options, a few articles each), so a branch-and-bound search is fast; the
    bound takes the smaller of "every option alone" and, per shared article, "this article's stock
    plus every option without it". MAX_NODES keeps a strange field from hanging a page: then the best
    mix found so far counts, never less than the best single option. Rejected: a greedy fill (wrong for
    the hose, it takes -L32 twice and leaves -L40 with nothing although 2+1 is possible) and an LP
    library (a new dependency for a handful of numbers). Tests: tests/domain/test_rules.py.
    """
    values = [_bundle_value(b, stock) for b in bundles]
    seen: set[int] = set()
    shared = False
    for bundle in bundles:
        for a, _ in bundle.needs:
            shared = shared or a in seen
            seen.add(a)
    if not shared:  # Part A's rule: every option gives on its own
        positive = sum(v for v in values if v > 0)
        return (positive if positive > 0 else max(values)), False
    if max(values) <= 0:
        return max(values), False
    articles = sorted(seen)
    least = {a: min(q for b in bundles for x, q in b.needs if x == a) for a in articles}
    best = max(values)
    nodes = 0
    capped = False

    def upper(i: int, left: Mapping[int, int]) -> int:
        alone = [max(0, _bundle_value(b, left)) for b in bundles[i:]]
        bound = sum(alone)
        for a in articles:
            without = sum(v for b, v in zip(bundles[i:], alone) if all(x != a for x, _ in b.needs))
            bound = min(bound, max(0, left[a]) // least[a] + without)
        return bound

    def search(i: int, left: dict[int, int], got: int) -> None:
        nonlocal best, nodes, capped
        nodes += 1
        if nodes > MAX_NODES:
            capped = True
            return
        if i == len(bundles):
            best = max(best, got)
            return
        if got + upper(i, left) <= best:
            return
        bundle = bundles[i]
        for y in range(max(0, _bundle_value(bundle, left)), -1, -1):
            after = dict(left)
            for a, q in bundle.needs:
                after[a] -= y * q
            search(i + 1, after, got + y)
            if capped:
                return

    search(0, dict(stock), 0)
    return best, capped


def bundle_availability(
    base: Sequence[tuple[int, int]],
    fields: Sequence[Sequence[Bundle]],
    available: Mapping[int, int],
) -> SetStock:
    """Availability from base parts (article_id, qty) and up to two choice fields of bundles (Spec 5.4,
    5.6; 0.4.0, p341). A variant is as often available as its own articles allow: the minimum over the
    base parts and its chosen bundles. Together, in any mix, it is the minimum over the base parts and
    what each field gives; the fields never share an article (Spec 5.6), so each is filled on its own.
    Tests: tests/domain/test_rules.py.
    """
    if any(not field for field in fields) or (not base and not fields):
        return SetStock(0, {})
    limits = [available[a] // q for a, q in base]
    totals = list(limits)
    capped = False
    for field in fields:
        most, field_capped = _field_most(field, available)
        totals.append(most)
        capped = capped or field_capped
    per_field = [[(b.key, _bundle_value(b, available)) for b in field] for field in fields]
    variants: dict[tuple[int, ...], int] = {}
    if per_field:
        for combination in itertools.product(*per_field):
            variants[tuple(k for k, _ in combination)] = min(limits + [v for _, v in combination])
    return SetStock(min(totals), variants, capped)


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


def reach_days(*, stock: float, coming: float, demand: float, per_day: float) -> int | None:
    """Reichweite in whole days (Spec 10.2, 0.4.2): what is there and on its way, less what the planned
    prints need, divided by the usage per day (Spec 9.2); rounded down, never below 0. No usage: None."""
    if per_day <= 0:
        return None
    return max(0, math.floor((stock + coming - demand) / per_day))


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
