"""Filament sorts, their stock and the daily record (Spec 4, 9.1, 9.2; B6 Entscheidungen 1-7).

Functions never commit; the caller owns the transaction.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.types import Spool
from reprintbuddy.domain import rules
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import Article, ArticleFilament, FilamentSnapshot, FilamentSort

WINDOW_DAYS = 30


@dataclass(frozen=True)
class SortStock:
    sort_id: int
    label: str
    material: str
    color_hex: str | None
    stock_g: float
    spools: int


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def _text(value: str | None) -> str:
    return (value or "").strip()


def sort_key(spool: Spool) -> str:
    """Brand, material, subtype and colour decide the sort; the colour name does not (Entscheidung 2)."""
    return "|".join(_norm(x) for x in (spool.brand, spool.material, spool.subtype, spool.color_hex))


def sort_label(spool: Spool) -> str:
    """Non-empty parts of brand, material, subtype, colour name; the hex stands in for a missing name."""
    name = _text(spool.color_name)
    if not name:
        last = _text(spool.color_hex)
    elif _norm(name) == _norm(spool.subtype):
        last = ""  # the name repeats the subtype: shown once, and no hex in its place
    else:
        last = name
    parts = [spool.brand, spool.material, spool.subtype, last]
    return " ".join(_text(p) for p in parts if _text(p))


def sync_sorts(session: Session, spools: Sequence[Spool]) -> dict[str, int]:
    """Create missing sorts, keep label and colour name current; returns key -> sort id."""
    ids: dict[str, int] = {}
    for spool in spools:
        key = sort_key(spool)
        sort = session.scalar(select(FilamentSort).where(FilamentSort.sort_key == key))
        label = sort_label(spool)
        if sort is None:
            sort = FilamentSort(
                sort_key=key,
                brand=spool.brand,
                material=spool.material,
                subtype=spool.subtype,
                color_hex=spool.color_hex,
                color_name=spool.color_name,
                label=label,
            )
            session.add(sort)
            session.flush()
        else:
            if _text(spool.color_name):  # a spool without a name never erases a known one
                sort.label = label
                sort.color_name = spool.color_name
            session.flush()
        ids[key] = sort.id
    return ids


def stock_by_sort(session: Session, spools: Sequence[Spool]) -> dict[int, SortStock]:
    """Live stock per sort: the summed `remaining_g` of the non-archived spools (Spec 9.1)."""
    live = [s for s in spools if not s.archived]
    ids = sync_sorts(session, live)
    grams: dict[int, float] = defaultdict(float)
    count: dict[int, int] = defaultdict(int)
    for spool in live:
        sort_id = ids[sort_key(spool)]
        grams[sort_id] += spool.remaining_g
        count[sort_id] += 1
    return {
        sort_id: _entry(session.get(FilamentSort, sort_id), grams[sort_id], count[sort_id])
        for sort_id in grams
    }


def _entry(sort: FilamentSort, grams: float, spools: int) -> SortStock:
    return SortStock(sort.id, sort.label, sort.material, sort.color_hex, grams, spools)


def stock_from_snapshots(session: Session) -> tuple[dict[int, SortStock], date | None]:
    """Stock from the rows of the latest recorded day; `({}, None)` without any record."""
    latest = session.scalar(select(func.max(FilamentSnapshot.day)))
    if latest is None:
        return {}, None
    grams: dict[int, float] = defaultdict(float)
    count: dict[int, int] = defaultdict(int)
    rows = session.scalars(select(FilamentSnapshot).where(FilamentSnapshot.day == latest))
    for row in rows:
        grams[row.spool_filament_id] += row.remaining_g
        count[row.spool_filament_id] += 1
    return {
        sort_id: _entry(session.get(FilamentSort, sort_id), grams[sort_id], count[sort_id])
        for sort_id in grams
    }, latest


def record_snapshot(session: Session, spools: Sequence[Spool], day: date) -> int:
    """One row per non-archived spool and day; a second call on the same day overwrites."""
    live = [s for s in spools if not s.archived]
    ids = sync_sorts(session, live)
    for spool in live:
        row = session.scalar(
            select(FilamentSnapshot).where(
                FilamentSnapshot.day == day, FilamentSnapshot.spool_id == spool.id
            )
        )
        if row is None:
            row = FilamentSnapshot(day=day, spool_id=spool.id)
            session.add(row)
        row.spool_filament_id = ids[sort_key(spool)]
        row.remaining_g = spool.remaining_g
        row.label_g = spool.label_g
        row.spool_created_at = spool.created_at
    # a spool recorded earlier today that is archived or gone now must not count any more
    gone = select(FilamentSnapshot).where(FilamentSnapshot.day == day)
    if live:
        gone = gone.where(FilamentSnapshot.spool_id.not_in([s.id for s in live]))
    for row in session.scalars(gone).all():
        session.delete(row)
    session.flush()
    return len(live)


def consumption(session: Session, sort_id: int, today: date) -> tuple[float, int]:
    """Grams per day and days of history over the last 30 days (Entscheidung 7)."""
    rows = session.execute(
        select(FilamentSnapshot.spool_id, FilamentSnapshot.day, FilamentSnapshot.remaining_g)
        .where(
            FilamentSnapshot.spool_filament_id == sort_id,
            FilamentSnapshot.day >= today - timedelta(days=WINDOW_DAYS),
            FilamentSnapshot.day <= today,
        )
        .order_by(FilamentSnapshot.day)
    )
    series: dict[int, list[tuple[date, float]]] = defaultdict(list)
    for spool_id, day, grams in rows:
        series[spool_id].append((day, grams))
    return rules.consumption_per_day(series)


def known_sorts(
    session: Session, spools: Sequence[Spool] | None
) -> tuple[dict[int, SortStock] | None, date | None]:
    """Stock of every known sort: live with `spools` (date None), else from the record.

    Sorts of the directory without stock show 0. `(None, None)`: nothing known (Anti-Ziel 4).
    """
    if spools is not None:
        stock, day = stock_by_sort(session, spools), None
    else:
        stock, day = stock_from_snapshots(session)
        if day is None:
            return None, None
    for sort in session.scalars(select(FilamentSort)):
        if sort.id not in stock:
            stock[sort.id] = _entry(sort, 0.0, 0)
    return stock, day


@dataclass(frozen=True)
class SlotRow:
    slot: int
    grams: float
    material: str | None
    color_hex: str | None
    mapped: int | None
    suggested: int | None


def suggest_sort(
    material: str | None, color_hex: str | None, sorts: Mapping[int, SortStock]
) -> int | None:
    """The one sort with the same material and colour; none or several give no suggestion (Spec 5.2)."""
    if not _norm(material) or not _norm(color_hex):
        return None
    hits = [
        sort_id
        for sort_id, sort in sorts.items()
        if _norm(sort.material) == _norm(material) and _norm(sort.color_hex) == _norm(color_hex)
    ]
    return hits[0] if len(hits) == 1 else None


def slot_rows(session: Session, article_id: int, sorts: Mapping[int, SortStock]) -> list[SlotRow]:
    """The filament slots of an article by slot number, with stored mapping and suggestion."""
    rows = session.scalars(
        select(ArticleFilament)
        .where(ArticleFilament.article_id == article_id)
        .order_by(ArticleFilament.slot)
    )
    return [
        SlotRow(
            r.slot,
            r.grams,
            r.material,
            r.color_hex,
            r.spool_filament_id,
            suggest_sort(r.material, r.color_hex, sorts),
        )
        for r in rows
    ]


def set_mapping(session: Session, article_id: int, mapping: Mapping[int, int | None]) -> None:
    """Store `slot -> sort id` (None clears); validates everything first, other slots stay."""
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    if article.archived_at is not None:
        raise ConflictError("articles.edit_archived")
    slots = {
        r.slot: r
        for r in session.scalars(select(ArticleFilament).where(ArticleFilament.article_id == article_id))
    }
    for slot, sort_id in mapping.items():
        if slot not in slots:
            raise ValidationError("filament.unknown_slot")
        if sort_id is not None and session.get(FilamentSort, sort_id) is None:
            raise NotFoundError("filament.sort_not_found")
    for slot, sort_id in mapping.items():
        slots[slot].spool_filament_id = sort_id
    session.flush()


def unmapped_slots(session: Session) -> list[tuple[int, str, int]]:
    """(article id, name, slot) of every slot without a sort, active articles only."""
    stmt = (
        select(Article.id, Article.name, ArticleFilament.slot)
        .join(ArticleFilament, ArticleFilament.article_id == Article.id)
        .where(Article.archived_at.is_(None), ArticleFilament.spool_filament_id.is_(None))
        .order_by(Article.name, ArticleFilament.slot)
    )
    return [(a, n, s) for a, n, s in session.execute(stmt)]
