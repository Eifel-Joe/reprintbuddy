"""All stock changes, each with its journal entry (Spec 6.2 to 6.4, 13).

This is the only module that changes `articles.stock`, `reserved`, `provisional`,
`awaiting_assembly` and `parts.stock` (tests/domain/test_single_stock_writer.py
checks that). Functions never commit: the caller opens the transaction.

Every booking first computes all new values and only then writes, so a booking
that would make a counter negative is rejected before anything has changed.
The one exception is the use of purchased parts (Spec 8.3): they are clamped to
0 and reported as `PartShortage`, because the parts are in the hand anyway.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain.errors import NotFoundError, StockLimitError, ValidationError
from reprintbuddy.models import Article, ArticlePart, JournalEntry, Part, utcnow

Actor = Literal["user", "auto"]

MAX_QTY = 1_000_000
MAX_NOTE = 500

# B5 adds its own kinds (with a translation `journal.kind.<kind>`).
KINDS: frozenset[str] = frozenset(
    {
        "order_created",
        "order_cancelled",
        "order_shipped",
        "sale",
        "stock_in",
        "inventory",
        "part_receipt",
        "print_finished",
        "inspection_good",
        "inspection_scrap",
        "assembly",
        "part_consumed",
        "queue_entry_created",
        "queue_entry_changed",
        "queue_entry_deleted",
        "queue_entry_held",
        "queue_entry_released",
        "print_dispatched",
        "print_dispatch_unclear",
        "print_returned",
        "print_failed",
        "print_cancelled",
        "print_missing",
        "print_duplicates",
        "automation_paused",
        "automation_resumed",
        "article_file_confirmed",
        "article_relinked",
    }
)


@dataclass(frozen=True)
class PartShortage:
    part_id: int
    needed: int
    had: int


@dataclass(frozen=True)
class _PartPlan:
    part: Part
    needed: int
    had: int

    @property
    def after(self) -> int:
        return max(0, self.had - self.needed)


# ---- checks and lookups --------------------------------------------------------------


def _number(value: object, *, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_QTY:
        raise ValidationError("form.invalid_number", field=field, min=minimum, max=MAX_QTY)
    return value


def _qty(n: object) -> int:
    return _number(n, field="bookings.field.qty", minimum=1)


def _counted(n: object) -> int:
    return _number(n, field="bookings.field.counted", minimum=0)


def _clean_note(note: str | None) -> str | None:
    if note is None:
        return None
    note = note.strip()
    if not note:
        return None
    if len(note) > MAX_NOTE:
        raise ValidationError("form.too_long", field="journal.field.note", max=MAX_NOTE)
    return note


def _booked(booked_at: datetime | None) -> datetime | None:
    """A booking may be dated back, never ahead (Spec 6.2, p172). Naive UTC like every time."""
    if booked_at is not None and booked_at > utcnow():
        raise ValidationError("bookings.date_future")
    return booked_at


def _article(session: Session, article_id: int) -> Article:
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    return article


def _part(session: Session, part_id: int) -> Part:
    part = session.get(Part, part_id)
    if part is None:
        raise NotFoundError("parts.not_found")
    return part


# ---- the journal ---------------------------------------------------------------------


def write_journal(
    session: Session,
    *,
    kind: str,
    actor: Actor,
    qty: int = 0,
    article_id: int | None = None,
    part_id: int | None = None,
    order_id: int | None = None,
    note: str | None = None,
    details: dict | None = None,
    booked_at: datetime | None = None,
) -> JournalEntry:
    """One journal line. Also used for actions without a stock change (B4).

    `booked_at`: when a hand booking counts if it is dated back (Spec 6.2, p172); `at` stays the
    time it is saved. Without it the booking counts when it is saved.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown journal kind: {kind}")
    now = utcnow()
    entry = JournalEntry(
        at=now,
        booked_at=now if booked_at is None else booked_at,
        actor=actor,
        kind=kind,
        article_id=article_id,
        part_id=part_id,
        order_id=order_id,
        qty=qty,
        note=_clean_note(note),
        details=details,
    )
    session.add(entry)
    session.flush()
    return entry


def journal_page(
    session: Session,
    *,
    article_id: int | None = None,
    part_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[JournalEntry]:
    """Newest first by the booking date: a booking dated back sorts in at its day (p172)."""
    query = select(JournalEntry).order_by(JournalEntry.booked_at.desc(), JournalEntry.id.desc())
    if article_id is not None:
        query = query.where(JournalEntry.article_id == article_id)
    if part_id is not None:
        query = query.where(JournalEntry.part_id == part_id)
    return list(session.scalars(query.limit(limit).offset(offset)))


# ---- planning (no writes) and applying -------------------------------------------------


def _plan_article(article: Article, deltas: Mapping[str, int]) -> dict[str, list[int]]:
    """New values of the counters as {field: [before, after]}; raises when one would go below 0."""
    changes: dict[str, list[int]] = {}
    for field, delta in deltas.items():
        before = getattr(article, field)
        after = before + delta
        if after < 0:
            raise StockLimitError(f"stock.limit.{field}", item=article.name)
        changes[field] = [before, after]
    return changes


def _apply(target: Article | Part, changes: Mapping[str, list[int]]) -> None:
    for field, (_before, after) in changes.items():
        setattr(target, field, after)


def _plan_parts(session: Session, article: Article, units: int) -> list[_PartPlan]:
    """The parts one stock addition of `units` consumes (Spec 6.6); never rejected."""
    rows = session.execute(
        select(ArticlePart, Part)
        .join(Part, Part.id == ArticlePart.part_id)
        .where(ArticlePart.article_id == article.id)
        .order_by(Part.id)
    ).all()
    return [_PartPlan(part, units * link.qty_per_unit, part.stock) for link, part in rows]


def _consume_parts(
    session: Session, plans: list[_PartPlan], article: Article, cause: str, actor: Actor,
    booked_at: datetime | None = None,
) -> list[PartShortage]:
    shortages: list[PartShortage] = []
    for plan in plans:
        shortage = max(0, plan.needed - plan.had)
        changes = {"stock": [plan.had, plan.after]}
        _apply(plan.part, changes)
        write_journal(
            session,
            kind="part_consumed",
            actor=actor,
            qty=plan.needed,
            article_id=article.id,
            part_id=plan.part.id,
            details={
                "cause": cause,
                "article_id": article.id,
                "shortage": shortage,
                "changes": changes,
            },
            booked_at=booked_at,  # the parts leave with the booking that uses them (Spec 9.2)
        )
        if shortage:
            shortages.append(PartShortage(plan.part.id, plan.needed, plan.had))
    return shortages


# ---- order bookings (Spec 6.2, 6.7) ----------------------------------------------------


def _order_booking(
    session: Session,
    kind: str,
    order_id: int,
    qty_by_article: Mapping[int, int],
    actor: Actor,
    note: str | None,
    sign_by_field: Mapping[str, int],
) -> None:
    note = _clean_note(note)
    quantities = {a: _qty(n) for a, n in qty_by_article.items()}
    articles = {a: _article(session, a) for a in sorted(quantities)}
    planned = {
        a: _plan_article(articles[a], {f: sign * quantities[a] for f, sign in sign_by_field.items()})
        for a in articles
    }
    for article_id, changes in planned.items():
        _apply(articles[article_id], changes)
        write_journal(
            session,
            kind=kind,
            actor=actor,
            qty=quantities[article_id],
            article_id=article_id,
            order_id=order_id,
            note=note,
            details={"changes": changes},
        )


def reserve_for_order(
    session: Session, order_id: int, qty_by_article: Mapping[int, int], actor: Actor,
    note: str | None = None,
) -> None:
    _order_booking(session, "order_created", order_id, qty_by_article, actor, note, {"reserved": 1})


def release_for_order(
    session: Session, order_id: int, qty_by_article: Mapping[int, int], actor: Actor,
    note: str | None = None,
) -> None:
    _order_booking(
        session, "order_cancelled", order_id, qty_by_article, actor, note, {"reserved": -1}
    )


def ship_for_order(
    session: Session, order_id: int, qty_by_article: Mapping[int, int], actor: Actor,
    note: str | None = None,
) -> None:
    _order_booking(
        session, "order_shipped", order_id, qty_by_article, actor, note,
        {"stock": -1, "reserved": -1},
    )


# ---- single article bookings -----------------------------------------------------------


def _simple(
    session: Session,
    kind: str,
    article_id: int,
    n: int,
    actor: Actor,
    note: str | None,
    deltas: Mapping[str, int],
    *,
    parts_cause: str | None = None,
    extra_details: dict | None = None,
    booked_at: datetime | None = None,
) -> list[PartShortage]:
    """One booking on one article, optionally consuming parts. Plan first, then write."""
    note = _clean_note(note)
    n = _qty(n)
    booked_at = _booked(booked_at)
    article = _article(session, article_id)
    changes = _plan_article(article, {f: sign * n for f, sign in deltas.items()})
    plans = _plan_parts(session, article, n) if parts_cause else []
    _apply(article, changes)
    write_journal(
        session,
        kind=kind,
        actor=actor,
        qty=n,
        article_id=article.id,
        note=note,
        details={**(extra_details or {}), "changes": changes},
        booked_at=booked_at,
    )
    return _consume_parts(session, plans, article, parts_cause, actor, booked_at) if parts_cause else []


def sell_unreserved(
    session: Session, article_id: int, n: int, actor: Actor, note: str | None = None, *,
    booked_at: datetime | None = None,
) -> None:
    """Sale without a reservation: stock − n (Spec 6.2). Available may become negative."""
    _simple(session, "sale", article_id, n, actor, note, {"stock": -1}, booked_at=booked_at)


def stock_in(
    session: Session,
    article_id: int,
    n: int,
    actor: Actor,
    note: str | None = None,
    *,
    consume_parts: bool,
    booked_at: datetime | None = None,
) -> list[PartShortage]:
    """Stock in without a print (Spec 6.2); with `consume_parts` the parts are used up (Spec 6.6)."""
    return _simple(
        session, "stock_in", article_id, n, actor, note, {"stock": 1},
        parts_cause="stock_in" if consume_parts else None, booked_at=booked_at,
    )


def add_provisional(
    session: Session, article_id: int, n: int, actor: Actor, *,
    note: str | None = None, details: dict | None = None,
) -> None:
    """A print finished: units become provisional (Spec 8.1)."""
    _simple(
        session, "print_finished", article_id, n, actor, note, {"provisional": 1},
        extra_details=details,
    )


def inspect_good(
    session: Session, article_id: int, n: int, actor: Actor, *, details: dict | None = None,
) -> list[PartShortage]:
    """Inspection good (Spec 8.2): provisional − n; stock + n, or awaiting assembly + n.
    No note: inspection and assembly book without one (Spec 6.2, p93)."""
    article = _article(session, article_id)
    if article.needs_assembly:
        return _simple(
            session, "inspection_good", article_id, n, actor, None,
            {"provisional": -1, "awaiting_assembly": 1}, extra_details=details,
        )
    return _simple(
        session, "inspection_good", article_id, n, actor, None,
        {"provisional": -1, "stock": 1}, parts_cause="inspection_good", extra_details=details,
    )


def inspect_scrap(
    session: Session, article_id: int, n: int, actor: Actor, *, details: dict | None = None,
) -> None:
    """Inspection scrap (Spec 8.2): provisional − n."""
    _simple(
        session, "inspection_scrap", article_id, n, actor, None, {"provisional": -1},
        extra_details=details,
    )


def assemble(
    session: Session, article_id: int, n: int, actor: Actor, *, details: dict | None = None,
) -> list[PartShortage]:
    """Assembly (Spec 8.3): awaiting assembly − n, stock + n, parts − n × quantity per unit."""
    return _simple(
        session, "assembly", article_id, n, actor, None,
        {"awaiting_assembly": -1, "stock": 1}, parts_cause="assembly", extra_details=details,
    )


# ---- inventory and parts -----------------------------------------------------------------


def count_article(
    session: Session,
    article_id: int,
    field: Literal["stock", "awaiting_assembly"],
    counted: int,
    actor: Actor,
    note: str | None = None,
    *,
    booked_at: datetime | None = None,
) -> int:
    """Inventory: sets the value to the counted quantity and books the difference (Spec 6.2).

    A date in the past only dates the row: the value is set now, bookings since then are not
    replayed (the counters keep no history to replay).
    """
    if field not in ("stock", "awaiting_assembly"):
        raise ValidationError("bookings.invalid_field", value=str(field))
    note = _clean_note(note)
    counted = _counted(counted)
    booked_at = _booked(booked_at)
    article = _article(session, article_id)
    before = getattr(article, field)
    difference = counted - before
    changes = {field: [before, counted]}
    _apply(article, changes)
    write_journal(
        session,
        kind="inventory",
        actor=actor,
        qty=difference,
        article_id=article.id,
        note=note,
        details={"field": field, "counted": counted, "changes": changes},
        booked_at=booked_at,
    )
    return difference


def receive_part(
    session: Session, part_id: int, n: int, actor: Actor, note: str | None = None, *,
    booked_at: datetime | None = None,
) -> None:
    """Goods receipt of a purchased part (Spec 6.2)."""
    note = _clean_note(note)
    n = _qty(n)
    booked_at = _booked(booked_at)
    part = _part(session, part_id)
    changes = {"stock": [part.stock, part.stock + n]}
    _apply(part, changes)
    write_journal(
        session, kind="part_receipt", actor=actor, qty=n, part_id=part.id, note=note,
        details={"changes": changes}, booked_at=booked_at,
    )


def count_part(
    session: Session, part_id: int, counted: int, actor: Actor, note: str | None = None, *,
    booked_at: datetime | None = None,
) -> int:
    """Inventory of a purchased part; also books the initial stock (Spec 5.3, 6.5)."""
    note = _clean_note(note)
    counted = _counted(counted)
    booked_at = _booked(booked_at)
    part = _part(session, part_id)
    difference = counted - part.stock
    changes = {"stock": [part.stock, counted]}
    _apply(part, changes)
    write_journal(
        session, kind="inventory", actor=actor, qty=difference, part_id=part.id, note=note,
        details={"field": "stock", "counted": counted, "changes": changes},
        booked_at=booked_at,
    )
    return difference
