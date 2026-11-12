"""Resolve a set through its inner sets into its articles (Spec 5.4, 0.4.3 p423). Pure, no database.

Since 0.4.3 a base row of a set holds an article or another set ("inner set"), nested to any depth.
Every place that turns a set into articles (availability, saving, the sets a set stands in, ordering,
the order page) goes through this module, so they cannot disagree. `catalog.load_defs` takes the
snapshot of all sets these functions read.

Since 0.4.4 (p441, Spec 5.4 "Mitgebrachte Wahlteile") an inner set brings its choice fields into the
outer set and the choice is made when ordering. `fields` lays them out flat: for each base row with
an inner set its fields (recursively), the quantity multiplied by the row, in the order of the rows,
then the set's own fields 1 and 2. A variant key holds one article per such field; `resolve` hands
each inner set its part of the key, so the order keeps the inner variant number as ordered.
Replaced: one fixed variant per inner set, stored in the row (0.4.3); it made a complete set with a
choice of three a matter of three sets.

Rules (Spec 5.4): quantities multiply along the levels; a set never holds itself, not even through
other sets (`Cycle`, with the way round); no article comes twice among the resolved base articles
and every option of every field, brought or own, and no inner set comes twice (`Twice`); a key names
one option of each field (`BadVariant`). Recursion ends through the way down it carries, not through
a depth limit ("beliebig tief"). Tests: tests/domain/test_setresolve.py.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from reprintbuddy.domain import rules


@dataclass(frozen=True)
class Base:
    """One base row as stored: an article or an inner set."""

    qty: int
    article_id: int | None = None
    set_id: int | None = None


@dataclass(frozen=True)
class Field:
    qty: int
    options: tuple[int, ...]  # article ids, in the order of the form
    suffixes: tuple[str | None, ...] = ()  # aligned with options


@dataclass(frozen=True)
class Slot:
    """One choice field of a set as it is ordered: its own or one an inner set brought."""

    qty: int  # per one piece of the set: the field's quantity times the rows on the way
    options: tuple[int, ...]
    suffixes: tuple[str | None, ...]  # aligned with options
    path: tuple[int, ...]  # the inner sets on the way down to the field's set, () for an own field


@dataclass(frozen=True)
class SetDef:
    id: int
    base: tuple[Base, ...]
    fields: tuple[Field, ...]
    name: str = ""
    sku: str | None = None


@dataclass(frozen=True)
class Step:
    """An inner set on the way down: `units` pieces of it go into one piece of the resolved set, in
    the variant `key` (its part of the ordered key; () when only base articles are resolved)."""

    set_id: int
    key: tuple[int, ...]
    units: int


@dataclass(frozen=True)
class Line:
    article_id: int
    qty: int  # per one piece of the resolved set
    path: tuple[Step, ...]  # () when the article stands directly in the resolved set


class ResolveError(Exception):
    pass


class Cycle(ResolveError):
    def __init__(self, path: tuple[int, ...]) -> None:
        super().__init__(path)
        self.path = path  # set ids, the first one again at the end


class Twice(ResolveError):
    def __init__(self, *, article_id: int | None = None, set_id: int | None = None) -> None:
        super().__init__(article_id, set_id)
        self.article_id = article_id
        self.set_id = set_id


class BadVariant(ResolveError):
    def __init__(self, set_id: int, key: tuple[int, ...]) -> None:
        super().__init__(set_id, key)
        self.set_id = set_id
        self.key = key


class Unknown(ResolveError):
    def __init__(self, set_id: int) -> None:
        super().__init__(set_id)
        self.set_id = set_id


class TooDeep(ResolveError):
    """Hundreds of levels exhaust Python's recursion: the set counts as broken instead of the page
    failing (review finding 7). No form builds this; it guards against stored data."""


def _get(defs: Mapping[int, SetDef], set_id: int) -> SetDef:
    found = defs.get(set_id)
    if found is None:
        raise Unknown(set_id)
    return found


def _child(defs: Mapping[int, SetDef], row: Base, trail: tuple[int, ...]) -> SetDef:
    """The inner set of a row; one already on the way down is a ring, a Cycle with its way round."""
    assert row.set_id is not None
    if row.set_id in trail:
        raise Cycle(trail[trail.index(row.set_id):] + (row.set_id,))
    return _get(defs, row.set_id)


def _fields(
    defs: Mapping[int, SetDef], set_id: int, factor: int, path: tuple[int, ...], trail: tuple[int, ...]
) -> Iterator[Slot]:
    for row in _get(defs, set_id).base:
        if row.set_id is not None:
            child = _child(defs, row, trail)
            yield from _fields(defs, child.id, factor * row.qty, path + (child.id,), trail + (child.id,))
    for field in _get(defs, set_id).fields:
        suffixes = tuple(field.suffixes) + (None,) * (len(field.options) - len(field.suffixes))
        yield Slot(factor * field.qty, field.options, suffixes, path)


def _deep(call):
    """Hundreds of levels exhaust Python's recursion: TooDeep instead of a crash (review finding 7)."""
    try:
        return call()
    except RecursionError:
        raise TooDeep() from None


def fields(defs: Mapping[int, SetDef], set_id: int) -> tuple[Slot, ...]:
    """Every choice field of the set: the brought ones in the order of the rows, then its own."""
    return _deep(lambda: tuple(_fields(defs, set_id, 1, (), (set_id,))))


def _base(
    defs: Mapping[int, SetDef], set_id: int, factor: int, path: tuple[Step, ...], trail: tuple[int, ...]
) -> Iterator[Line]:
    for row in _get(defs, set_id).base:
        if row.set_id is None:
            assert row.article_id is not None
            yield Line(row.article_id, factor * row.qty, path)
            continue
        child = _child(defs, row, trail)
        units = factor * row.qty
        yield from _base(defs, child.id, units, path + (Step(child.id, (), units),), trail + (child.id,))


def _all(
    defs: Mapping[int, SetDef],
    set_id: int,
    key: tuple[int, ...],
    factor: int,
    path: tuple[Step, ...],
    trail: tuple[int, ...],
) -> Iterator[Line]:
    """The articles of one variant; `key` is exactly this set's part (one article per field of it)."""
    at = 0
    found = _get(defs, set_id)
    for row in found.base:
        if row.set_id is None:
            assert row.article_id is not None
            yield Line(row.article_id, factor * row.qty, path)
            continue
        child = _child(defs, row, trail)
        size = len(tuple(_fields(defs, child.id, 1, (), trail + (child.id,))))
        part = key[at:at + size]
        at += size
        units = factor * row.qty
        yield from _all(defs, child.id, part, units, path + (Step(child.id, part, units),), trail + (child.id,))
    for field in found.fields:
        yield Line(key[at], factor * field.qty, path)
        at += 1


def _valid_key(slots: tuple[Slot, ...], key: tuple[int, ...]) -> bool:
    return len(key) == len(slots) and all(k in slot.options for k, slot in zip(key, slots))


def resolve_base(defs: Mapping[int, SetDef], set_id: int) -> tuple[Line, ...]:
    """The articles of the base rows, inner sets resolved, per one piece of the set; no choice parts."""
    _get(defs, set_id)
    return _deep(lambda: tuple(_base(defs, set_id, 1, (), (set_id,))))


def resolve(defs: Mapping[int, SetDef], set_id: int, key: tuple[int, ...] = ()) -> tuple[Line, ...]:
    """All articles of one variant: `key` holds the chosen article per field of `fields` (() without)."""
    if not _valid_key(fields(defs, set_id), tuple(key)):
        raise BadVariant(set_id, tuple(key))
    return _deep(lambda: tuple(_all(defs, set_id, tuple(key), 1, (), (set_id,))))


def _inner_sets(defs: Mapping[int, SetDef], set_id: int) -> Iterator[int]:
    """Every inner set row on every level, once per row (the ring check ran before)."""
    for row in _get(defs, set_id).base:
        if row.set_id is not None:
            yield row.set_id
            yield from _inner_sets(defs, row.set_id)


def _possible(defs: Mapping[int, SetDef], set_id: int) -> list[int]:
    base = resolve_base(defs, set_id)
    return [line.article_id for line in base] + [o for slot in fields(defs, set_id) for o in slot.options]


def possible_articles(defs: Mapping[int, SetDef], set_id: int) -> set[int]:
    """Every article the set may hold: its resolved base articles and every option of every field."""
    return set(_possible(defs, set_id))


def check(defs: Mapping[int, SetDef], set_id: int) -> None:
    """Refuse a ring, an unknown inner set, an inner set twice and an article twice; every option of
    every field counts, brought ones too (Spec 5.4, 0.4.4)."""
    possible = _possible(defs, set_id)
    seen_sets: set[int] = set()
    for inner in _deep(lambda: list(_inner_sets(defs, set_id))):
        if inner in seen_sets:
            raise Twice(set_id=inner)
        seen_sets.add(inner)
    seen: set[int] = set()
    for article_id in possible:
        if article_id in seen:
            raise Twice(article_id=article_id)
        seen.add(article_id)


def direct_parents(defs: Mapping[int, SetDef], set_id: int) -> list[int]:
    """The sets that hold this set in one of their own base rows."""
    return sorted(s.id for s in defs.values() if any(row.set_id == set_id for row in s.base))


def containing(defs: Mapping[int, SetDef], set_id: int) -> list[int]:
    """Every set that holds this set on any level (the set itself only through a stored ring)."""
    found: set[int] = set()
    todo = [set_id]
    while todo:
        for parent in direct_parents(defs, todo.pop()):
            if parent not in found:
                found.add(parent)
                todo.append(parent)
    return sorted(found)


def variant_number(defs: Mapping[int, SetDef], set_id: int, key: tuple[int, ...]) -> str | None:
    """The number of a variant: the set's number with the suffixes of all its fields, brought ones
    first (Spec 5.4). None without choices, for a key that is no variant, and while the set number
    or a suffix is missing."""
    slots = fields(defs, set_id)
    if not slots or not _valid_key(slots, tuple(key)):
        return None
    suffixes = [slot.suffixes[slot.options.index(chosen)] for chosen, slot in zip(key, slots)]
    return rules.variant_number(_get(defs, set_id).sku, suffixes)
