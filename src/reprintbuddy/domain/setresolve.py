"""Resolve a set through its inner sets into its articles (Spec 5.4, 0.4.3 p423). Pure, no database.

Since 0.4.3 a base row of a set holds an article or another set ("inner set"), the latter in a fixed
variant when it has choice parts, nested to any depth. Every place that turns a set into articles
(availability, saving, the sets a set stands in, ordering, the order page) goes through this module,
so they cannot disagree. `catalog.load_defs` takes the snapshot of all sets these functions read.

Rules (Spec 5.4): quantities multiply along the levels; a set never holds itself, not even through
other sets (`Cycle`, with the way round); no article comes twice in the resolved articles of a set,
counting every option of the set's own choice fields as well, and no inner set comes twice, not even
as two variants (`Twice`); the fixed variant of an inner set names one article per choice field of
that set (`BadVariant`). The options of an inner set that its fixed variant does not choose are not
part of the outer set, so they may stand there on their own (plan approval v0.4.3).
Recursion ends through the way down it carries, not through a depth limit ("beliebig tief").
Tests: tests/domain/test_setresolve.py.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from reprintbuddy.domain import rules


@dataclass(frozen=True)
class Base:
    """One base row as stored: an article or an inner set with its fixed variant."""

    qty: int
    article_id: int | None = None
    set_id: int | None = None
    key: tuple[int, ...] = ()  # the chosen article per choice field of the inner set


@dataclass(frozen=True)
class Field:
    qty: int
    options: tuple[int, ...]  # article ids, in the order of the form
    suffixes: tuple[str | None, ...] = ()  # aligned with options


@dataclass(frozen=True)
class SetDef:
    id: int
    base: tuple[Base, ...]
    fields: tuple[Field, ...]
    name: str = ""
    sku: str | None = None


@dataclass(frozen=True)
class Step:
    """An inner set on the way down: `units` pieces of it go into one piece of the resolved set."""

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


def _get(defs: Mapping[int, SetDef], set_id: int) -> SetDef:
    found = defs.get(set_id)
    if found is None:
        raise Unknown(set_id)
    return found


def _valid_key(found: SetDef, key: tuple[int, ...]) -> bool:
    return len(key) == len(found.fields) and all(k in f.options for k, f in zip(key, found.fields))


def _check_key(found: SetDef, key: tuple[int, ...]) -> None:
    if not _valid_key(found, key):
        raise BadVariant(found.id, key)


def _base(
    defs: Mapping[int, SetDef], set_id: int, factor: int, path: tuple[Step, ...], trail: tuple[int, ...]
) -> Iterator[Line]:
    for row in _get(defs, set_id).base:
        if row.set_id is None:
            assert row.article_id is not None
            yield Line(row.article_id, factor * row.qty, path)
            continue
        if row.set_id in trail:
            raise Cycle(trail[trail.index(row.set_id):] + (row.set_id,))
        child = _get(defs, row.set_id)
        _check_key(child, row.key)
        units = factor * row.qty
        yield from _all(defs, child.id, row.key, units, path + (Step(child.id, row.key, units),), trail + (child.id,))


def _all(
    defs: Mapping[int, SetDef],
    set_id: int,
    key: tuple[int, ...],
    factor: int,
    path: tuple[Step, ...],
    trail: tuple[int, ...],
) -> Iterator[Line]:
    yield from _base(defs, set_id, factor, path, trail)
    for field, chosen in zip(_get(defs, set_id).fields, key):
        yield Line(chosen, factor * field.qty, path)


def resolve_base(defs: Mapping[int, SetDef], set_id: int) -> tuple[Line, ...]:
    """The articles of the base rows, inner sets resolved, per one piece of the set."""
    _get(defs, set_id)
    return tuple(_base(defs, set_id, 1, (), (set_id,)))


def resolve(defs: Mapping[int, SetDef], set_id: int, key: tuple[int, ...] = ()) -> tuple[Line, ...]:
    """All articles of one variant (`key`: the chosen article per own choice field; () without)."""
    _check_key(_get(defs, set_id), key)
    return tuple(_all(defs, set_id, key, 1, (), (set_id,)))


def _inner_sets(defs: Mapping[int, SetDef], set_id: int) -> Iterator[int]:
    """Every inner set row on every level, once per row (the ring check ran before)."""
    for row in _get(defs, set_id).base:
        if row.set_id is not None:
            yield row.set_id
            yield from _inner_sets(defs, row.set_id)


def check(defs: Mapping[int, SetDef], set_id: int) -> None:
    """Refuse a ring, an unknown inner set, a wrong fixed variant, an inner set twice and an article twice."""
    base = resolve_base(defs, set_id)
    seen_sets: set[int] = set()
    for inner in _inner_sets(defs, set_id):
        if inner in seen_sets:
            raise Twice(set_id=inner)
        seen_sets.add(inner)
    seen: set[int] = set()
    options = [option for field in _get(defs, set_id).fields for option in field.options]
    for article_id in [line.article_id for line in base] + options:
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
    """The number of a variant of a set with choice parts; None without choices, for a key that is
    no variant, and while the set number or a suffix is missing (Spec 5.4)."""
    found = _get(defs, set_id)
    if not found.fields or not _valid_key(found, key):
        return None
    suffixes = []
    for chosen, field in zip(key, found.fields):
        index = field.options.index(chosen)
        suffixes.append(field.suffixes[index] if index < len(field.suffixes) else None)
    return rules.variant_number(found.sku, suffixes)
