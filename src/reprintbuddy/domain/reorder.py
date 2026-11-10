"""Planned demand (Spec 9.2), reorder suggestions and "ordered" marks (Spec 9.3, 9.4).

Functions never commit; the caller owns the transaction. The demand functions only read.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy.activity.capture import acting
from reprintbuddy.bambuddy.types import Spool
from reprintbuddy.domain import filament, rules, workqueue
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import (
    Article,
    ArticleFilament,
    ArticlePart,
    FilamentSettings,
    FilamentSnapshot,
    FilamentSort,
    JournalEntry,
    Part,
    ReorderMark,
    utcnow,
)

WINDOW_DAYS = 30
MAX_PACKS = 1000


def missing_to_target(article: Article, coverage_total: int) -> int:
    """Units still missing up to the target quantity (never negative)."""
    return max(0, rules.target_quantity(article.min_stock, article.buffer) - coverage_total)


def _missing_by_article(session: Session) -> tuple[dict[int, Article], dict[int, int]]:
    articles = {
        a.id: a for a in session.scalars(select(Article).where(Article.archived_at.is_(None)))
    }
    coverage = workqueue.coverage_all(session)
    return articles, {i: missing_to_target(a, coverage[i].total) for i, a in articles.items()}


def filament_demand(session: Session) -> dict[int, float]:
    """Grams per reordered spool sort: over the slots where it is reordered, grams x (queued +
    in-progress prints + prints up to the target) (Spec 9.2).

    Counts every non-archived article (also with automation off); slots without a reordered sort
    are skipped; sorts without demand may be missing from the result.
    """
    articles, missing = _missing_by_article(session)
    queued = workqueue.queued_prints(session)
    running = workqueue.in_progress_prints(session)
    grams = {(r.article_id, r.slot): r.grams for r in session.scalars(select(ArticleFilament))}
    demand: dict[int, float] = defaultdict(float)
    for group in filament.reorder_groups(session).values():
        for article_id, slot in group.slots:
            article = articles.get(article_id)
            if article is None or (article_id, slot) not in grams:
                continue
            prints = (
                queued.get(article_id, 0)
                + running.get(article_id, 0)
                + rules.prints_for_units(missing[article_id], article.units_per_print)
            )
            demand[group.sort_id] += grams[(article_id, slot)] * prints
    return dict(demand)


def part_demand(session: Session) -> dict[int, int]:
    """Units per purchased part: qty_per_unit x (awaiting assembly + queued units + units in
    progress + units up to the target). "Vorläufig" is deliberately not a summand (Spec 9.2)."""
    articles, missing = _missing_by_article(session)
    queued = workqueue.queued_prints(session)
    units = workqueue.in_progress_units(session)
    demand: dict[int, int] = defaultdict(int)
    for link in session.scalars(select(ArticlePart)):
        article = articles.get(link.article_id)
        if article is None:
            continue
        per_unit = (
            article.awaiting_assembly
            + queued.get(article.id, 0) * article.units_per_print
            + units.get(article.id, 0)
            + missing[article.id]
        )
        demand[link.part_id] += link.qty_per_unit * per_unit
    return dict(demand)


# ---- consumption of a purchased part (Entscheidung 8) -----------------------------------------


def part_consumption(session: Session, part_id: int, today: date) -> float:
    """Units per day from the bookings of the last 30 days that took the part off (Spec 9.2, 6.6).

    Divided by the days since the oldest row of the whole journal, at most 30 and at least 1;
    0.0 for an empty journal. Every `part_consumed` row counts, whatever booking wrote it:
    assembly, "inspection good" of an article without hand work, stock in without a print with
    "deduct parts" ticked (p174: before, only assembly counted, so the parts of articles without
    hand work had a usage of 0 and their suggestions came too late). Rows count by their booking
    date, not by when they were saved (p172).
    """
    oldest = session.scalar(select(func.min(JournalEntry.booked_at)))
    if oldest is None:
        return 0.0
    since = datetime.combine(today - timedelta(days=WINDOW_DAYS), datetime.min.time())
    rows = session.scalars(
        select(JournalEntry).where(
            JournalEntry.kind == "part_consumed",
            JournalEntry.part_id == part_id,
            JournalEntry.booked_at >= since,
        )
    )
    used = sum(r.qty for r in rows)
    days = max(1, min(WINDOW_DAYS, (today - oldest.date()).days))
    return used / days


# ---- suggestions (Spec 9.3) -------------------------------------------------------------------


@dataclass(frozen=True)
class Suggestion:
    item_type: Literal["filament", "part"]
    item_id: int
    label: str
    unit: str
    stock: float
    demand: float
    per_day: float
    lead_days: int
    safety: float
    packs: int
    pack_size: float
    supplier: str | None
    order_url: str | None
    has_history: bool = True  # False: no usage recorded yet, the tile says so instead of "0.0 g/day" (p59)
    on_way: float = 0.0  # ordered and not arrived yet (Spec 9.4, p171); counts like stock, shown apart


def _ordered(session: Session, mark: ReorderMark) -> float:
    """What a mark ordered, in the item's unit: packs x pack size (Spec 9.4)."""
    if mark.item_type == "part":
        part = session.get(Part, mark.item_id)
        size = part.pack_size if part is not None else 0
    else:
        settings = session.get(FilamentSettings, mark.item_id)
        # without settings a mark counts in the default unit, as "on its way" shows it (open_marks)
        size = settings.pack_grams if settings is not None else filament.FilamentSettingsInput.pack_grams
    return mark.qty * size


@dataclass(frozen=True)
class _NewSpool:
    sort_id: int | None
    label_g: float
    created_at: datetime | None


def _spools_seen(session: Session, spools: Sequence[Spool] | None) -> list[_NewSpool]:
    """The non-archived spools Reprintbuddy knows: Bambuddy's list, without it the latest record.

    Filament is never booked in Reprintbuddy; Bambuddy keeps it, Reprintbuddy only reads the spool
    list (Spec 9.1, 9.4). The record keeps sort, nominal weight and creation time of each spool.
    """
    if spools is not None:
        live = [s for s in spools if not s.archived]
        sort_of = filament.resolve_sorts(session, live)
        return [_NewSpool(sort_of[s.id], s.label_g, s.created_at) for s in live]
    latest = session.scalar(select(func.max(FilamentSnapshot.day)))
    if latest is None:
        return []
    rows = session.scalars(select(FilamentSnapshot).where(FilamentSnapshot.day == latest))
    return [_NewSpool(r.spool_filament_id, r.label_g, r.spool_created_at) for r in rows]


def _part_receipts(session: Session, mark: ReorderMark) -> list[tuple[datetime, int]]:
    """Receipts of the part entered after the mark, oldest first: (saved at, quantity)."""
    rows = session.execute(
        select(JournalEntry.at, JournalEntry.qty)
        .where(
            JournalEntry.kind == "part_receipt",
            JournalEntry.part_id == mark.item_id,
            JournalEntry.at > mark.marked_at,
        )
        .order_by(JournalEntry.at, JournalEntry.id)
    )
    return [(at, qty) for at, qty in rows]


def _arrived(session: Session, mark: ReorderMark, seen: Sequence[_NewSpool]) -> float:
    """What of a mark has arrived, in the item's unit (Spec 9.4, p211): for a part the receipts
    after the mark, for filament the nominal weight of new spools of the sort (created after the
    mark; a new roll already in use has arrived all the same)."""
    if mark.item_type == "part":
        return float(sum(qty for _at, qty in _part_receipts(session, mark)))
    return float(sum(
        s.label_g
        for s in seen
        if s.sort_id == mark.item_id and s.created_at is not None and _naive_utc(s.created_at) > mark.marked_at
    ))


def on_way(session: Session, spools: Sequence[Spool] | None = None) -> dict[tuple[str, int], float]:
    """What counts as on its way per item, in its unit: per open mark the ordered amount less what
    has arrived of it (Spec 9.4). `spools`: Bambuddy's list; None takes the latest record.

    p171: an open mark counts, also in the group of another reordered sort. p211: before, the full
    amount counted until the mark closed, so of 3 rolls ordered 2 arrived ones counted twice (in
    the stock and on its way) and a suggestion came late; a part mark closed with the first
    receipt, so the rest counted nowhere and a suggestion came early. Now what arrived is taken off.
    """
    seen = _spools_seen(session, spools)
    amounts: dict[tuple[str, int], float] = {}
    for mark in session.scalars(select(ReorderMark).where(ReorderMark.received_at.is_(None))):
        rest = max(0.0, _ordered(session, mark) - _arrived(session, mark, seen))
        if rest > 0:
            key = (mark.item_type, mark.item_id)
            amounts[key] = amounts.get(key, 0.0) + rest
    return amounts


def _open_marked(session: Session) -> set[tuple[str, int]]:
    rows = session.execute(
        select(ReorderMark.item_type, ReorderMark.item_id).where(ReorderMark.received_at.is_(None))
    )
    return {(kind, item) for kind, item in rows}


def _suggest(
    kind: Literal["filament", "part"], item_id: int, label: str, unit: str, *, stock: float,
    demand: float, per_day: float, lead_days: int, safety: float, pack: float, min_packs: int,
    supplier: str | None, order_url: str | None, has_history: bool = True, coming: float = 0.0,
) -> Suggestion | None:
    have = stock + coming  # what is on its way counts like stock (Spec 9.4)
    if not rules.needs_reorder(
        stock=have, demand=demand, per_day=per_day, lead_days=lead_days, safety=safety
    ):
        return None
    packs = rules.suggest_packs(
        stock=have, demand=demand, per_day=per_day, lead_days=lead_days, safety=safety,
        pack=pack, min_packs=min_packs,
    )
    return Suggestion(
        kind, item_id, label, unit, stock, demand, per_day, lead_days, safety, packs, pack,
        supplier, order_url, has_history, coming,
    )


def suggestions(
    session: Session, *, sorts: Mapping[int, filament.SortStock] | None, today: date,
    spools: Sequence[Spool] | None = None,
) -> list[Suggestion]:
    """Reorder suggestions: reordered spool sorts with settings (only when the stock is known, i.e.
    `sorts` is not None), then every active part. Items with an open mark have none (Entscheidung 14);
    what is on its way counts like stock, also in the group of another sort (Spec 9.4, p171).

    A reordered spool sort counts, over the slots where it is reordered, the stock and the usage of
    every spool sort ticked there (Spec 9.2); a sort ticked in the slots of two reordered sorts
    counts for both. `spools` tells what of an order has arrived (p211); None: the latest record.
    """
    marked = _open_marked(session)
    coming = on_way(session, spools)
    result: list[Suggestion] = []
    if sorts is not None:
        demand = filament_demand(session)
        groups = filament.reorder_groups(session)
        rows = session.execute(
            select(FilamentSettings, FilamentSort)
            .join(FilamentSort, FilamentSort.id == FilamentSettings.spool_filament_id)
            .where(FilamentSort.id.in_(list(groups)))
            .order_by(FilamentSort.label, FilamentSort.id)
        )
        for settings, sort in rows:
            if ("filament", sort.id) in marked:
                continue
            members = groups[sort.id].members
            per_day, days = filament.consumption(session, members, today)
            have = sum(sorts[m].stock_g for m in members if m in sorts)
            sug = _suggest(
                "filament", sort.id, sort.label, "g", stock=have, demand=demand.get(sort.id, 0.0),
                coming=sum(coming.get(("filament", m), 0.0) for m in members),
                per_day=per_day, lead_days=settings.lead_time_days,
                safety=rules.safety_amount(settings.safety_value, settings.safety_unit, per_day),
                pack=settings.pack_grams, min_packs=settings.min_order_packs,
                supplier=settings.supplier, order_url=settings.order_url, has_history=days > 0,
            )
            if sug is not None:
                result.append(sug)
    part_needs = part_demand(session)
    part_history = session.scalar(select(func.min(JournalEntry.booked_at))) is not None
    for part in session.scalars(
        select(Part).where(Part.archived_at.is_(None)).order_by(Part.name, Part.id)
    ):
        if ("part", part.id) in marked:
            continue
        per_day = part_consumption(session, part.id, today)
        sug = _suggest(
            "part", part.id, part.name, part.unit, stock=part.stock,
            demand=part_needs.get(part.id, 0), per_day=per_day, lead_days=part.lead_time_days,
            safety=rules.reorder_floor(
                rules.safety_amount(part.safety_value, part.safety_unit, per_day), part.min_stock
            ),
            pack=part.pack_size, min_packs=part.min_order_packs, supplier=part.supplier,
            order_url=part.order_url, has_history=part_history,
            coming=coming.get(("part", part.id), 0.0),  # 0 here, a part with an open mark is skipped
        )
        if sug is not None:
            result.append(sug)
    return result


@dataclass(frozen=True)
class Current:
    """What the reorder page shows, computed in one place for the page and its key figure (p64)."""

    sorts: dict[int, filament.SortStock] | None
    day: date | None  # the recorded day the filament stock comes from; None when live or unknown
    suggestions: list[Suggestion]


def current(session: Session, spools: Sequence[Spool] | None, now: datetime) -> Current:
    """Stock (live with `spools`, else the record), close arrived marks, then the suggestions.
    It writes (new sorts, closed marks): call it in a write transaction."""
    sorts, day = filament.known_sorts(session, spools)
    close_arrived_marks(session, spools, now)
    return Current(sorts, day, suggestions(session, sorts=sorts, today=now.date(), spools=spools))


def count_suggestions(
    session: Session, spools: Sequence[Spool] | None, now: datetime, *, write: bool = True
) -> int:
    """The number of suggestions the reorder page shows (the key figure of Spec 9.4).

    `write=False` writes nothing, for the time writes are paused (a restore or an update): the stock
    comes from the record (no new spool sorts are entered) and arrived marks stay open until the
    next count that may write. The number can lag behind then, never the stored data.
    """
    if not write:
        sorts, _day = filament.known_sorts(session, None)
        return len(suggestions(session, sorts=sorts, today=now.date()))
    return len(current(session, spools, now).suggestions)


# ---- "ordered" marks (Spec 9.4, Entscheidung 14, 17) ------------------------------------------


def _open_mark_for(session: Session, item_type: str, item_id: int) -> ReorderMark | None:
    return session.scalar(
        select(ReorderMark).where(
            ReorderMark.item_type == item_type,
            ReorderMark.item_id == item_id,
            ReorderMark.received_at.is_(None),
        )
    )


def mark_ordered(session: Session, item_type: str, item_id: int, packs: int) -> ReorderMark:
    """Mark a part or filament sort as ordered, `packs` in order units. No journal row."""
    if isinstance(packs, bool) or not isinstance(packs, int) or not 1 <= packs <= MAX_PACKS:
        raise ValidationError("reorder.invalid_packs")
    if item_type == "part":
        part = session.get(Part, item_id)
        known = part is not None and part.archived_at is None
    elif item_type == "filament":
        sort = session.get(FilamentSort, item_id)  # a legacy sort of 0006 is no spool sort to order
        known = sort is not None and not sort.legacy and session.get(FilamentSettings, item_id) is not None
    else:
        known = False
    if not known:
        raise NotFoundError("reorder.item_not_found")
    if _open_mark_for(session, item_type, item_id) is not None:
        raise ConflictError("reorder.already_marked")
    mark = ReorderMark(item_type=item_type, item_id=item_id, qty=packs)
    session.add(mark)
    session.flush()
    return mark


def mark_received(session: Session, mark_id: int) -> None:
    mark = session.get(ReorderMark, mark_id)
    if mark is None:
        raise NotFoundError("reorder.mark_not_found")
    if mark.received_at is not None:
        raise ConflictError("reorder.already_received")
    mark.received_at = utcnow()
    session.flush()


@dataclass(frozen=True)
class MarkRow:
    id: int
    item_type: str
    item_id: int
    label: str
    packs: int
    pack_size: float
    unit: str
    marked_at: datetime
    arrived: float = 0.0  # what of it has arrived so far (p211), in `unit`


def open_marks(session: Session, spools: Sequence[Spool] | None = None) -> list[MarkRow]:
    """The open marks (on its way), oldest first; those of archived parts are left out.
    `spools`: Bambuddy's list for what has arrived; None takes the latest record."""
    seen = _spools_seen(session, spools)
    rows: list[MarkRow] = []
    marks = session.scalars(
        select(ReorderMark)
        .where(ReorderMark.received_at.is_(None))
        .order_by(ReorderMark.marked_at, ReorderMark.id)
    )
    for mark in marks:
        if mark.item_type == "part":
            part = session.get(Part, mark.item_id)
            if part.archived_at is not None:
                continue  # archived parts leave every list (Spec 5.5); the mark stays for a restore (p60)
            label, size, unit = part.name, part.pack_size, part.unit
        else:
            sort = session.get(FilamentSort, mark.item_id)
            settings = session.get(FilamentSettings, mark.item_id)
            size = settings.pack_grams if settings is not None else filament.FilamentSettingsInput.pack_grams
            label, unit = sort.label, "g"
        rows.append(
            MarkRow(
                mark.id, mark.item_type, mark.item_id, label, mark.qty, size, unit, mark.marked_at,
                _arrived(session, mark, seen),
            )
        )
    return rows


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def close_arrived_marks(session: Session, spools: Sequence[Spool] | None, now: datetime) -> int:
    """Close open marks whose goods have arrived; returns how many (Entscheidung 14).

    A part mark closes when the `part_receipt` rows after the mark reach the ordered amount (p211;
    before, the first receipt closed it). A filament mark closes only with a spool list: new spools
    of the sort (created after the mark, not archived, with a creation time) must add up to at
    least `qty x pack_grams` of net weight.
    """
    # Reprintbuddy closes them by its rule, also when a page asks (Spec 9.4, 10.7: "Automatik").
    with acting(session, "auto"):
        return _close_arrived_marks(session, spools, now)


def _close_arrived_marks(session: Session, spools: Sequence[Spool] | None, now: datetime) -> int:
    closed = 0
    seen = None if spools is None else _spools_seen(session, spools)
    for mark in session.scalars(select(ReorderMark).where(ReorderMark.received_at.is_(None))).all():
        if mark.item_type == "part":
            # p211: the mark closes when the receipts reach the ordered amount, at the one that did
            ordered, total = _ordered(session, mark), 0
            for at, qty in _part_receipts(session, mark):
                total += qty
                if total >= ordered:
                    mark.received_at = at
                    closed += 1
                    break
        elif seen is not None:
            if session.get(FilamentSettings, mark.item_id) is None or session.get(FilamentSort, mark.item_id) is None:
                continue
            if _arrived(session, mark, seen) >= _ordered(session, mark):
                mark.received_at = now
                closed += 1
    session.flush()
    return closed
