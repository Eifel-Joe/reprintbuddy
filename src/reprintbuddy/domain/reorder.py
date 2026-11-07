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

from reprintbuddy.bambuddy.types import Spool
from reprintbuddy.domain import filament, rules, workqueue
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import (
    Article,
    ArticleFilament,
    ArticlePart,
    FilamentSettings,
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
    """Grams per filament sort: grams x (queued + in-progress prints + prints up to the target).

    Counts every non-archived article (also with automation off); slots without a sort are
    skipped; sorts without demand may be missing from the result.
    """
    articles, missing = _missing_by_article(session)
    queued = workqueue.queued_prints(session)
    running = workqueue.in_progress_prints(session)
    demand: dict[int, float] = defaultdict(float)
    for slot in session.scalars(
        select(ArticleFilament).where(ArticleFilament.spool_filament_id.is_not(None))
    ):
        article = articles.get(slot.article_id)
        if article is None:
            continue
        prints = (
            queued.get(article.id, 0)
            + running.get(article.id, 0)
            + rules.prints_for_units(missing[article.id], article.units_per_print)
        )
        demand[slot.spool_filament_id] += slot.grams * prints
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
    """Units per day from the assembly bookings of the last 30 days (Spec 9.2).

    Divided by the days since the oldest row of the whole journal, at most 30 and at least 1;
    0.0 for an empty journal. Only `part_consumed` rows with the cause "assembly" count.
    """
    oldest = session.scalar(select(func.min(JournalEntry.at)))
    if oldest is None:
        return 0.0
    since = datetime.combine(today - timedelta(days=WINDOW_DAYS), datetime.min.time())
    rows = session.scalars(
        select(JournalEntry).where(
            JournalEntry.kind == "part_consumed",
            JournalEntry.part_id == part_id,
            JournalEntry.at >= since,
        )
    )
    used = sum(r.qty for r in rows if (r.details or {}).get("cause") == "assembly")
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


def _open_marked(session: Session) -> set[tuple[str, int]]:
    rows = session.execute(
        select(ReorderMark.item_type, ReorderMark.item_id).where(ReorderMark.received_at.is_(None))
    )
    return {(kind, item) for kind, item in rows}


def _suggest(
    kind: Literal["filament", "part"], item_id: int, label: str, unit: str, *, stock: float,
    demand: float, per_day: float, lead_days: int, safety: float, pack: float, min_packs: int,
    supplier: str | None, order_url: str | None,
) -> Suggestion | None:
    if not rules.needs_reorder(
        stock=stock, demand=demand, per_day=per_day, lead_days=lead_days, safety=safety
    ):
        return None
    packs = rules.suggest_packs(
        stock=stock, demand=demand, per_day=per_day, lead_days=lead_days, safety=safety,
        pack=pack, min_packs=min_packs,
    )
    return Suggestion(
        kind, item_id, label, unit, stock, demand, per_day, lead_days, safety, packs, pack,
        supplier, order_url,
    )


def suggestions(
    session: Session, *, sorts: Mapping[int, filament.SortStock] | None, today: date
) -> list[Suggestion]:
    """Reorder suggestions: filament sorts with settings (only when the stock is known, i.e.
    `sorts` is not None), then every active part. Items with an open mark have none (Entscheidung 14)."""
    marked = _open_marked(session)
    result: list[Suggestion] = []
    if sorts is not None:
        demand = filament_demand(session)
        rows = session.execute(
            select(FilamentSettings, FilamentSort)
            .join(FilamentSort, FilamentSort.id == FilamentSettings.spool_filament_id)
            .order_by(FilamentSort.label, FilamentSort.id)
        )
        for settings, sort in rows:
            if ("filament", sort.id) in marked:
                continue
            per_day = filament.consumption(session, sort.id, today)[0]
            have = sorts[sort.id].stock_g if sort.id in sorts else 0.0
            sug = _suggest(
                "filament", sort.id, sort.label, "g", stock=have, demand=demand.get(sort.id, 0.0),
                per_day=per_day, lead_days=settings.lead_time_days,
                safety=rules.safety_amount(settings.safety_value, settings.safety_unit, per_day),
                pack=settings.pack_grams, min_packs=settings.min_order_packs,
                supplier=settings.supplier, order_url=settings.order_url,
            )
            if sug is not None:
                result.append(sug)
    part_needs = part_demand(session)
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
            order_url=part.order_url,
        )
        if sug is not None:
            result.append(sug)
    return result


def count_suggestions(session: Session, today: date) -> int:
    """Number of suggestions with the stock from the daily record (no record: parts only)."""
    stock, day = filament.stock_from_snapshots(session)
    return len(suggestions(session, sorts=stock if day is not None else None, today=today))


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
        known = session.get(FilamentSettings, item_id) is not None
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


def open_marks(session: Session) -> list[MarkRow]:
    rows: list[MarkRow] = []
    marks = session.scalars(
        select(ReorderMark)
        .where(ReorderMark.received_at.is_(None))
        .order_by(ReorderMark.marked_at, ReorderMark.id)
    )
    for mark in marks:
        if mark.item_type == "part":
            part = session.get(Part, mark.item_id)
            label, size, unit = part.name, part.pack_size, part.unit
        else:
            sort = session.get(FilamentSort, mark.item_id)
            settings = session.get(FilamentSettings, mark.item_id)
            label, size, unit = sort.label, settings.pack_grams, "g"
        rows.append(
            MarkRow(mark.id, mark.item_type, mark.item_id, label, mark.qty, size, unit, mark.marked_at)
        )
    return rows


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def close_arrived_marks(session: Session, spools: Sequence[Spool] | None, now: datetime) -> int:
    """Close open marks whose goods have arrived; returns how many (Entscheidung 14).

    A part mark closes with the first `part_receipt` row after the mark. A filament mark closes
    only with a spool list: new spools of the sort (created after the mark, not archived, with a
    creation time) must add up to at least `qty x pack_grams` of net weight.
    """
    closed = 0
    live = None if spools is None else [s for s in spools if not s.archived]
    for mark in session.scalars(select(ReorderMark).where(ReorderMark.received_at.is_(None))).all():
        if mark.item_type == "part":
            arrived = session.scalar(
                select(func.min(JournalEntry.at)).where(
                    JournalEntry.kind == "part_receipt",
                    JournalEntry.part_id == mark.item_id,
                    JournalEntry.at > mark.marked_at,
                )
            )
            if arrived is not None:
                mark.received_at = arrived
                closed += 1
        elif live is not None:
            settings = session.get(FilamentSettings, mark.item_id)
            sort = session.get(FilamentSort, mark.item_id)
            if settings is None or sort is None:
                continue
            grams = sum(
                s.label_g
                for s in live
                if s.created_at is not None
                and _naive_utc(s.created_at) > mark.marked_at
                and filament.sort_key(s) == sort.sort_key
            )
            if grams >= mark.qty * settings.pack_grams:
                mark.received_at = now
                closed += 1
    session.flush()
    return closed
