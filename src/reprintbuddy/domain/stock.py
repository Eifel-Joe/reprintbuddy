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
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain.errors import NotFoundError, StockLimitError, ValidationError
from reprintbuddy.localtime import end_of_local_day, to_local
from reprintbuddy.models import Article, ArticleOption, ArticlePart, JournalEntry, Part, utcnow

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
        "assembled",  # Montage aus Teilen, on the holder (0.4.0, p341)
        "built_in",  # a part built into an article made of parts
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
    if booked_at is not None:
        if "stock" in changes:
            _refuse_if_counted_later(session, article, booked_at)
        for plan in plans:
            _refuse_if_counted_later(session, plan.part, booked_at)
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
    _holder(session, article_id)  # an article made of parts on order sells through sell_from_parts
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
    """Stock in without a print (Spec 6.2); with `consume_parts` the parts are used up (Spec 6.6).
    Only print articles: one made of parts comes in by its assembly or a count (Spec 6.2, 0.4.0)."""
    if _article(session, article_id).kind != "print":
        raise ValidationError("bookings.print_only")
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

    Dated back, the count holds for that time: the difference is taken against the value of then
    (see `_count`), bookings since then stay.
    """
    if field not in ("stock", "awaiting_assembly"):
        raise ValidationError("bookings.invalid_field", value=str(field))
    note = _clean_note(note)
    counted = _counted(counted)
    booked_at = _booked(booked_at)
    article = _holder(session, article_id)
    own_rows = (JournalEntry.article_id == article.id, JournalEntry.part_id.is_(None))
    return _count(
        session, article, field, counted, actor, note, booked_at, own_rows, article_id=article.id
    )


def _change(row: JournalEntry, field: str) -> int:
    """How much a journal row changed `field`; every counter booking keeps its before/after in
    `details["changes"]` (this module is the only writer)."""
    pair = ((row.details or {}).get("changes") or {}).get(field)
    return pair[1] - pair[0] if isinstance(pair, list) and len(pair) == 2 else 0


def _replay(
    target: Article | Part, field: str, counted: int, now_value: int, later: list[JournalEntry]
) -> tuple[int, int]:
    """(value then, value now) for a count dated before the `later` rows (oldest first).

    The value then is today's value less every later change, as booked. From the count the later
    rows are played forward again: a use of parts takes its full amount, capped at 0 (Spec 8.3) -
    its recorded change may have been capped against a lower value. Review finding: adding only
    the difference got that wrong (a use capped at 0 took nothing after a higher count) and missed
    a later booking that the count would have made go below 0; that is refused (Spec 6.4).
    """
    then = now_value - sum(_change(row, field) for row in later)
    value = counted
    for row in later:
        if isinstance(target, Part) and row.kind == "part_consumed":
            value = max(0, value - row.qty)
        else:
            value += _change(row, field)
        if value < 0:
            raise StockLimitError(f"stock.limit.{field}", item=target.name)
    return then, value


def _holds_until(row: JournalEntry) -> datetime:
    """When an inventory row holds: a count dated back until the end of its day (Spec 6.2, p260),
    one of "today" at its moment.

    p301: counts dated back with 0.3.14 and 0.3.15 are stored at the clock time they were typed in on
    that day, so a sale dated to that day and typed in later in the day slipped past the check of
    p261. They are told by their two times: a booking of "today" is saved with the same time for
    when it counts (`write_journal`, migration 0013), one dated back counts on an earlier day.
    Rejected alternative: move the old rows to the end of their day - a booking of that day after
    the count would then lie in it, and today's stock would have to change by it without knowing
    when the count was really made. Tests: tests/domain/test_old_backdated_inventory.py.

    Review findings: a row of 0.3.16 or later already sits at the end of its day (`time.max`, the
    only time with 999999 microseconds a clock time hits once in a million); rounded again, a later
    change of the server zone moved it onto the next day and blocked that whole day. And the old rows
    took the offset of the day of typing (fixed offset of the machine zone, see `at_local_time`): a
    count typed at 23:30 in winter for a summer day lies on the next day in summer time; that offset,
    added back, gives the day that was picked.
    """
    if row.booked_at == row.at or row.booked_at.microsecond == 999_999:
        return row.booked_at
    picked = (row.booked_at + to_local(row.at).utcoffset()).date()
    return end_of_local_day(picked)


def _inventories_after(
    session: Session, target_rows: tuple, field: str, booked_at: datetime
) -> list[JournalEntry]:
    """The inventories of `field` that hold after `booked_at` (`_holds_until`), oldest first.

    A count holds at most until the end of its own day, and a day lasts at most 25 hours, so only
    rows from shortly before `booked_at` on can hold after it; two days leave room for that.
    """
    rows = session.scalars(
        select(JournalEntry)
        .where(
            *target_rows,
            JournalEntry.kind == "inventory",
            JournalEntry.booked_at > booked_at - timedelta(days=2),
        )
        .order_by(JournalEntry.booked_at, JournalEntry.id)
    )
    later = [
        r for r in rows
        if (r.details or {}).get("field", "stock") == field and _holds_until(r) > booked_at
    ]
    return sorted(later, key=lambda r: (_holds_until(r), r.id))


def _own_rows(target: Article | Part) -> tuple:
    if isinstance(target, Part):
        return (JournalEntry.part_id == target.id,)
    return (JournalEntry.article_id == target.id, JournalEntry.part_id.is_(None))


def _refuse_if_counted_later(session: Session, target: Article | Part, booked_at: datetime) -> None:
    """A booking dated back that changes the stock of `target` is refused when that stock was
    counted after its date (Spec 6.2, p261): the count already holds it.

    Root of p261: the booking changed today's value although the later count had set it anew, so
    a sale forgotten three days ago took 2 off the 10 counted yesterday. Rejected alternative:
    book it as held in the count without a change - a row with a quantity that changes nothing,
    and with parts taken off a stock in, one part of a booking would count and another not.
    Only the stock is checked: the dated bookings (sale, stock in, receipt of parts, the parts a
    stock in takes off) change nothing else. Tests: tests/domain/test_booked_before_inventory.py.
    """
    later = _inventories_after(session, _own_rows(target), "stock", booked_at)
    if later:
        # the day the count holds for: for an old row stored across summer time the day that was picked
        raise ValidationError("bookings.counted_later", item=target.name, at=_holds_until(later[0]))


def _count(
    session: Session,
    target: Article | Part,
    field: str,
    counted: int,
    actor: Actor,
    note: str | None,
    booked_at: datetime | None,
    target_rows: tuple,
    **ids: int,
) -> int:
    """Set `field` of `target` by a count; returns the difference booked (Spec 6.2, p210).

    Today: value := counted. Dated back: the count holds for that time. Root of p210: the value was
    set to the count now, so bookings between that day and now (a sale this morning) were lost and
    the journal no longer added up. Now the difference is taken against the value of then and the
    later bookings are played forward from the count (`_replay`). Rejected (rather than replayed) when an inventory of the same field lies
    after the date; going below 0 is refused as always (Spec 6.4). Rejected alternative: inventory
    only for today - the date field would mean nothing there and a count of yesterday had to be
    worked out by hand.
    """
    before = getattr(target, field)
    details: dict = {"field": field, "counted": counted}
    if booked_at is None:
        difference, after = counted - before, counted
    else:
        later = list(session.scalars(
            select(JournalEntry)
            .where(*target_rows, JournalEntry.booked_at > booked_at)
            .order_by(JournalEntry.booked_at, JournalEntry.id)
        ))
        later_count = [
            r for r in later if r.kind == "inventory" and (r.details or {}).get("field", "stock") == field
        ]
        if later_count:
            raise ValidationError("bookings.later_inventory", at=later_count[0].booked_at)
        then, after = _replay(target, field, counted, before, later)
        difference = counted - then
        details["then"] = [then, counted]
    if after < 0:
        raise StockLimitError(f"stock.limit.{field}", item=target.name)
    changes = {field: [before, after]}
    _apply(target, changes)
    details["changes"] = changes
    write_journal(
        session, kind="inventory", actor=actor, qty=difference, note=note, details=details,
        booked_at=booked_at, **ids,
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
    if booked_at is not None:
        _refuse_if_counted_later(session, part, booked_at)
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
    """Inventory of a purchased part; also books the initial stock (Spec 5.3, 6.5). Dated back, like
    `count_article` (p210)."""
    note = _clean_note(note)
    counted = _counted(counted)
    booked_at = _booked(booked_at)
    part = _part(session, part_id)
    return _count(
        session, part, "stock", counted, actor, note, booked_at, (JournalEntry.part_id == part.id,),
        part_id=part.id,
    )


# ---- articles made of parts (Spec 5.6, 6.2, 8.3; 0.4.0 p341) -------------------------------------


def _options_of(session: Session, composite_id: int) -> bool:
    return session.scalar(select(ArticleOption.id).where(ArticleOption.article_id == composite_id).limit(1)) is not None


def is_stock_holder(session: Session, article: Article) -> bool:
    """A row that holds finished stock (Spec 5.6, 6.1): a print article, an article made of parts built
    on stock without choices, or a variant row of one built on stock. Archived or not."""
    if article.kind == "print":
        return True
    if article.kind == "assembled":
        return article.assembly_mode == "stock" and not _options_of(session, article.id)
    parent = session.get(Article, article.parent_id)
    return parent is not None and parent.assembly_mode == "stock"


def _holder(session: Session, article_id: int) -> Article:
    article = _article(session, article_id)
    if not is_stock_holder(session, article):
        raise ValidationError("bookings.not_a_holder", item=article.name)
    return article


def _part_rows(session: Session, needs: Mapping[int, int], n: int) -> list[tuple[Article, int]]:
    """(print article, units) per part of `n` pieces, by id."""
    return [(_article(session, part_id), n * qty) for part_id, qty in sorted(needs.items())]


def build_from_parts(
    session: Session,
    holder_id: int,
    n: int,
    needs: Mapping[int, int],
    composite_id: int,
    actor: Actor,
) -> list[PartShortage]:
    """Montage aus Teilen (Spec 6.2, 8.3): the holder + n; each part - n x qty, only from its available
    stock (stock - reserved, Spec 8.3 / question 5: a part reserved for an order is never built in);
    the purchased parts of the article made of parts - n x qty per unit, clamped at 0 (Spec 8.3).
    Refused before anything is written when a part has too little available."""
    n = _qty(n)
    holder = _holder(session, holder_id)
    composite = _article(session, composite_id)
    parts = _part_rows(session, needs, n)
    for part, units in parts:
        available = part.stock - part.reserved
        if units > available:
            raise ValidationError("assembled.part_short", name=part.name, available=available, needed=units)
    holder_changes = _plan_article(holder, {"stock": n})
    part_changes = [(part, units, _plan_article(part, {"stock": -units})) for part, units in parts]
    plans = _plan_parts(session, composite, n)
    _apply(holder, holder_changes)
    write_journal(
        session, kind="assembled", actor=actor, qty=n, article_id=holder.id,
        details={"composite_id": composite.id, "variant_sku": holder.sku, "changes": holder_changes},
    )
    for part, units, changes in part_changes:
        _apply(part, changes)
        write_journal(
            session, kind="built_in", actor=actor, qty=units, article_id=part.id,
            details={"into": holder.id, "composite_id": composite.id, "variant_sku": holder.sku, "changes": changes},
        )
    return _consume_parts(session, plans, composite, "assembled", actor)


def sell_from_parts(
    session: Session,
    composite_id: int,
    needs: Mapping[int, int],
    n: int,
    actor: Actor,
    note: str | None = None,
    *,
    variant_sku: str | None = None,
    booked_at: datetime | None = None,
) -> list[PartShortage]:
    """Sale without a reservation of an article made of parts built on order (Spec 6.2, Teil C 4): the
    stock of each part - n x qty and its purchased parts - n x qty per unit. Dated back, it is refused
    when a part or a purchased part was counted after that date (Spec 6.2, p261)."""
    note = _clean_note(note)
    n = _qty(n)
    booked_at = _booked(booked_at)
    composite = _article(session, composite_id)
    parts = _part_rows(session, needs, n)
    part_changes = [(part, units, _plan_article(part, {"stock": -units})) for part, units in parts]
    plans = _plan_parts(session, composite, n)
    if booked_at is not None:
        for part, _units in parts:
            _refuse_if_counted_later(session, part, booked_at)
        for plan in plans:
            _refuse_if_counted_later(session, plan.part, booked_at)
    write_journal(
        session, kind="sale", actor=actor, qty=n, article_id=composite.id, note=note,
        details={"variant_sku": variant_sku, "changes": {}}, booked_at=booked_at,
    )
    for part, units, changes in part_changes:
        _apply(part, changes)
        write_journal(
            session, kind="sale", actor=actor, qty=units, article_id=part.id, note=note,
            details={"assembled_id": composite.id, "variant_sku": variant_sku, "changes": changes},
            booked_at=booked_at,
        )
    return _consume_parts(session, plans, composite, "sale_assembled", actor, booked_at)


def consume_assembled_parts(
    session: Session, composite_id: int, units: int, order_id: int, actor: Actor, *, variant_sku: str | None = None,
) -> list[PartShortage]:
    """Shipping an article made of parts built on order is its assembly (Spec 6.2, 6.6): its purchased
    parts go with it. The parts themselves leave with the order lines (`ship_for_order`)."""
    composite = _article(session, composite_id)
    write_journal(
        session, kind="order_shipped", actor=actor, qty=units, article_id=composite.id, order_id=order_id,
        details={"variant_sku": variant_sku, "changes": {}},
    )
    return _consume_parts(session, _plan_parts(session, composite, units), composite, "shipped_assembled", actor)
