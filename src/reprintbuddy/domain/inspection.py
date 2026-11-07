"""Inspection of finished prints (Spec 8.1) and assembly (Spec 8.3).

A finished print books its units as provisional and opens an inspection in the same
transaction (plan B4 decision 18); B5 builds the count of good pieces on it. Assembly
turns units waiting for assembly into stock. Functions never commit: the caller opens
the transaction. All counter changes go through `reprintbuddy.domain.stock`, the only
writer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import stock, workqueue
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.domain.rules import prints_for_units
from reprintbuddy.domain.stock import Actor, PartShortage, add_provisional
from reprintbuddy.models import Article, ArticlePart, Dispatch, Inspection, Part, utcnow

# ---- inspection of a finished print (Spec 8.1) ---------------------------------------


def open_inspection(session: Session, dispatch: Dispatch) -> Inspection:
    """Units of the finished `dispatch` become provisional, waiting for the inspection."""
    inspection = Inspection(
        dispatch_id=dispatch.id, article_id=dispatch.article_id, units=dispatch.units
    )
    session.add(inspection)
    session.flush()
    add_provisional(
        session, dispatch.article_id, dispatch.units, "auto", details={"dispatch_id": dispatch.id}
    )
    return inspection


# ---- the count of good pieces (plan B5 task 1; Spec 7.5, 8.2) -------------------------------


@dataclass(frozen=True)
class InspectionRow:
    id: int
    article_id: int
    article_name: str
    units: int
    created_at: datetime
    needs_assembly: bool
    has_thumbnail: bool


@dataclass(frozen=True)
class ResolveResult:
    inspection_id: int
    good: int
    scrap: int
    shortages: list[PartShortage]
    replacement_entry_id: int | None


@dataclass(frozen=True)
class AllGoodResult:
    resolved: int
    skipped: int
    shortages: list[PartShortage]


def list_open(session: Session, article_id: int | None = None) -> list[InspectionRow]:
    """The prints waiting for inspection, oldest first; only those of `article_id` if given."""
    query = (
        select(
            Inspection.id,
            Inspection.article_id,
            Article.name,
            Inspection.units,
            Inspection.created_at,
            Article.needs_assembly,
            Article.thumbnail.is_not(None),
        )
        .join(Article, Article.id == Inspection.article_id)
        .where(Inspection.resolved_at.is_(None))
        .order_by(Inspection.created_at, Inspection.id)
    )
    if article_id is not None:
        query = query.where(Inspection.article_id == article_id)
    return [InspectionRow(*row) for row in session.execute(query).all()]


def _current(session: Session, inspection_id: int) -> Inspection | None:
    """The stored state of an inspection, read anew.

    Callers open their transaction as a write transaction (`begin_immediate`), so two
    requests for the same inspection run one after the other: the second one reads the
    state the first one left and is turned away by the `resolved_at` check.
    """
    return session.get(Inspection, inspection_id, populate_existing=True)


def _book(
    session: Session, row: Inspection, good: int, actor: Actor, note: str | None
) -> ResolveResult:
    """Book `good` units as good and the rest as scrap; scrap is replaced at the queue start."""
    article = session.get(Article, row.article_id)
    scrap = row.units - good
    details = {"inspection_id": row.id, "dispatch_id": row.dispatch_id}
    shortages: list[PartShortage] = []
    if good > 0:
        shortages = stock.inspect_good(session, row.article_id, good, actor, note, details=details)
    replacement_id = None
    if scrap > 0:
        stock.inspect_scrap(session, row.article_id, scrap, actor, note, details=details)
        # The current units per print count, not the ones at hand-over (plan B5 decision 2);
        # a replacement is wanted whatever the automation or a pause says (Spec 7.5).
        # An article with an open inspection is never archived (archiving refuses while units
        # are provisional), so add_replacement cannot refuse it here.
        replacement = workqueue.add_replacement(
            session,
            row.article_id,
            prints_for_units(scrap, article.units_per_print),
            "scrap",
            actor,
            details=details,
        )
        replacement_id = replacement.id
    row.good_units = good
    row.resolved_at = utcnow()
    session.flush()
    return ResolveResult(row.id, good, scrap, shortages, replacement_id)


def resolve(
    session: Session, inspection_id: int, good_units: int, actor: Actor, note: str | None = None
) -> ResolveResult:
    """Confirm how many units of a finished print are good (Spec 8.2)."""
    row = _current(session, inspection_id)
    if row is None:
        raise NotFoundError("inspections.not_found")
    if row.resolved_at is not None:
        raise ConflictError("inspections.already_resolved")
    if (
        isinstance(good_units, bool)
        or not isinstance(good_units, int)
        or not 0 <= good_units <= row.units
    ):
        raise ValidationError(
            "form.invalid_number", field="inspections.field.good", min=0, max=row.units
        )
    return _book(session, row, good_units, actor, note)


def resolve_scrap(
    session: Session, inspection_id: int, scrap_units: int, actor: Actor, note: str | None = None
) -> ResolveResult:
    """Confirm a print by its scrap: good units = the units of the print minus `scrap_units`.

    This is the input of the list (Spec 8.2: "Ausschuss", default 0). The units of the print are
    read here, inside the caller's write transaction, so the split is always against the print as
    it stands now. The booking is the one of `resolve`.
    """
    row = _current(session, inspection_id)
    if row is None:
        raise NotFoundError("inspections.not_found")
    if row.resolved_at is not None:
        raise ConflictError("inspections.already_resolved")
    if (
        isinstance(scrap_units, bool)
        or not isinstance(scrap_units, int)
        or not 0 <= scrap_units <= row.units
    ):
        raise ValidationError(
            "form.invalid_number", field="inspections.field.scrap", min=0, max=row.units
        )
    return _book(session, row, row.units - scrap_units, actor, note)


def resolve_all_good(
    session: Session,
    inspection_ids: Sequence[int],
    actor: Actor,
    article_id: int | None = None,
) -> AllGoodResult:
    """All units good for each given inspection; unknown or already inspected ones are skipped.

    With `article_id` only the prints of that article are touched (the filtered list): the check
    is made here against the stored print, whatever the request carries. Prints of other articles
    are no part of the request and are not counted as skipped.
    """
    resolved = skipped = 0
    shortages: list[PartShortage] = []
    for inspection_id in inspection_ids:
        row = _current(session, inspection_id)
        if row is not None and article_id is not None and row.article_id != article_id:
            continue
        if row is None or row.resolved_at is not None:
            skipped += 1
            continue
        shortages += _book(session, row, row.units, actor, None).shortages
        resolved += 1
    return AllGoodResult(resolved, skipped, shortages)


# ---- assembly (Spec 8.3) -------------------------------------------------------------


@dataclass(frozen=True)
class PartNeed:
    part_id: int
    name: str
    qty_per_unit: int
    stock: int


@dataclass(frozen=True)
class AssemblyRow:
    article_id: int
    name: str
    awaiting: int
    parts: list[PartNeed]
    has_thumbnail: bool = False


def list_awaiting_assembly(session: Session, article_id: int | None = None) -> list[AssemblyRow]:
    """Active articles with units waiting for assembly, by name, with their parts; only
    `article_id` if given (the filtered list).

    Not filtered by `needs_assembly`: units already waiting must stay bookable
    after the flag is switched off (decision 4).
    """
    query = (
        select(Article)
        .where(Article.archived_at.is_(None), Article.awaiting_assembly > 0)
        .order_by(Article.name, Article.id)
    )
    if article_id is not None:
        query = query.where(Article.id == article_id)
    articles = session.scalars(query).all()
    rows: list[AssemblyRow] = []
    for article in articles:
        needs = session.execute(
            select(Part, ArticlePart.qty_per_unit)
            .join(ArticlePart, ArticlePart.part_id == Part.id)
            .where(ArticlePart.article_id == article.id)
            .order_by(Part.name, Part.id)
        ).all()
        rows.append(
            AssemblyRow(
                article_id=article.id,
                name=article.name,
                awaiting=article.awaiting_assembly,
                parts=[PartNeed(p.id, p.name, qty, p.stock) for p, qty in needs],
                has_thumbnail=article.thumbnail is not None,
            )
        )
    return rows


def assemble(
    session: Session, article_id: int, n: int, actor: Actor, note: str | None = None
) -> list[PartShortage]:
    """Books an assembly through the stock module; it checks the limits (decision 4)."""
    return stock.assemble(session, article_id, n, actor, note)
