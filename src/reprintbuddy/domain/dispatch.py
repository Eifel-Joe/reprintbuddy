"""Handing prints from Reprintbuddy's work queue over to Bambuddy (Spec 7.3, 7.7).

Plan B4 decisions 1-5: one print at a time, each with its own batch in Bambuddy.

1. `reserve_next` takes one print off the top eligible entry in a short write
   transaction and creates the `dispatches` row in state `sending`.
2. `hand_over` calls Bambuddy outside any transaction: `ensure_batch`, optionally
   `list_queue`, then - after `enqueue_sent_at` has been committed - `enqueue`.

The order in step 2 is the safety of the whole automation: once `enqueue_sent_at` is
set, nothing here puts the print back into the work queue except a 4xx answer
(Bambuddy said for certain it did not take it). An unclear answer leaves the print
`unclear`; the tick looks for it in Bambuddy and never sends it again (decision 5).
Any other exception after the mark leaves it `sending` with the mark set, which the
tick treats the same way.

The session functions never commit: the caller opens the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import EnqueueRequest, QueueItem
from reprintbuddy.domain.pauses import pause_article
from reprintbuddy.domain.stock import write_journal
from reprintbuddy.domain.tracking import apply_outcome, classify
from reprintbuddy.domain.workqueue import OPEN_STATES, _ordered, _renumber
from reprintbuddy.models import Article, Dispatch, WorkQueueEntry, utcnow

HandOverResult = Literal["queued", "unclear", "not_sent", "rejected"]
MAX_MESSAGE = 500


@dataclass(frozen=True)
class Reservation:
    dispatch_id: int
    article_id: int
    external_ref: str
    batch_name: str
    library_file_id: int
    plate_id: int
    target_model: str | None
    printer_id: int | None


def _dispatch(session: Session, dispatch_id: int) -> Dispatch:
    row = session.get(Dispatch, dispatch_id)
    if row is None:
        raise ValueError(f"unknown dispatch {dispatch_id}")
    return row


def open_count(session: Session) -> int:
    """Prints that occupy Bambuddy or may do so (decision 2: `sending` and `unclear` count)."""
    return int(
        session.scalar(
            select(func.count()).select_from(Dispatch).where(Dispatch.status.in_(OPEN_STATES))
        )
    )


def reserve_next(
    session: Session, *, cap: int, skip_article_ids: Collection[int] = frozenset()
) -> Reservation | None:
    """Take one print off the top eligible entry, or None (cap reached, nothing eligible).

    Skipped without a change: held entries, paused or archived articles, and the
    articles in `skip_article_ids` (their printer is only temporarily inactive).
    An article without any print target is paused (`no_target`) and skipped.
    """
    if open_count(session) >= cap:
        return None
    entries = _ordered(session)
    for entry in entries:
        if entry.held:
            continue
        article = session.get(Article, entry.article_id)
        if article is None or article.paused or article.archived_at is not None:
            continue
        if article.id in skip_article_ids:
            continue
        if not article.target_model and article.printer_id is None:
            pause_article(session, article.id, "no_target", "auto")
            continue
        return _reserve(session, entries, entry, article)
    return None


def _reserve(
    session: Session, entries: list[WorkQueueEntry], entry: WorkQueueEntry, article: Article
) -> Reservation:
    row = Dispatch(
        article_id=article.id,
        origin=entry.origin,
        external_ref=uuid.uuid4().hex,
        status="sending",
        units=article.units_per_print,
        queue_entry_id=entry.id,
        queue_position=entry.position,
    )
    session.add(row)
    if entry.prints > 1:
        entry.prints -= 1
    else:
        session.delete(entry)
        session.flush()
        _renumber(session, [e for e in entries if e is not entry])
    session.flush()
    # The chosen printer wins over the model, as on the article page (catalog.target_value).
    printer_id = article.printer_id
    target_model = None if printer_id is not None else article.target_model
    return Reservation(
        dispatch_id=row.id,
        article_id=article.id,
        external_ref=row.external_ref,
        batch_name=f"{article.name} ({row.id})",
        library_file_id=article.library_file_id,
        plate_id=article.plate_id,
        target_model=target_model,
        printer_id=printer_id,
    )


def next_insert_position(
    queue: Sequence[QueueItem], own_item_ids: Collection[int]
) -> tuple[bool, int | None]:
    """Where the next own print goes with "insert next" (Spec 7.3).

    Behind the last own waiting entry, so own prints keep their order; to the top when
    no own entry waits. Returns `(insert_at_top, insert_position)`.
    """
    own = [i for i in queue if i.id in own_item_ids and i.status == "pending"]
    if not own:
        return True, None
    positions = [i.position for i in own if i.position is not None]
    if not positions:
        # Own entries wait but Bambuddy names no position: appending keeps them in front.
        return False, None
    return False, max(positions) + 1


def own_item_ids(session: Session) -> set[int]:
    """Bambuddy queue ids of all open prints."""
    return set(
        session.scalars(
            select(Dispatch.bambuddy_item_id).where(
                Dispatch.status.in_(OPEN_STATES), Dispatch.bambuddy_item_id.is_not(None)
            )
        )
    )


# ---- state changes ---------------------------------------------------------------------


def mark_sent(session: Session, dispatch_id: int) -> None:
    """Note that `enqueue` is about to be sent; refuses a second mark (no second send)."""
    row = _dispatch(session, dispatch_id)
    if row.status != "sending" or row.enqueue_sent_at is not None:
        raise ValueError(f"dispatch {dispatch_id} cannot be sent ({row.status})")
    row.enqueue_sent_at = utcnow()
    session.flush()


def mark_unclear(session: Session, dispatch_id: int) -> None:
    """`enqueue` was sent, the answer is unknown: the print may be in Bambuddy."""
    row = _dispatch(session, dispatch_id)
    if row.status == "unclear":
        return
    if row.status != "sending":
        raise ValueError(f"dispatch {dispatch_id} cannot become unclear ({row.status})")
    row.status = "unclear"
    session.flush()
    write_journal(
        session,
        kind="print_dispatch_unclear",
        actor="auto",
        qty=row.units,
        article_id=row.article_id,
        details={"dispatch_id": row.id, "batch_id": row.bambuddy_batch_id},
    )


def confirm_dispatch(session: Session, dispatch_id: int, item: QueueItem) -> None:
    """Bambuddy holds the print as `item`; its state follows `tracking.classify`.

    The print becomes `queued` and is handed over (journal), then Bambuddy's state is
    applied like a poll answer: `printing`, or - for an entry found already finished -
    provisional stock, replacement or pause. An unknown state leaves it `queued`; the
    next poll reports it.
    """
    row = _dispatch(session, dispatch_id)
    if row.status not in ("sending", "unclear"):
        raise ValueError(f"dispatch {dispatch_id} cannot be confirmed ({row.status})")
    row.bambuddy_item_id = item.id
    if row.bambuddy_batch_id is None:
        row.bambuddy_batch_id = item.batch_id
    row.last_status = item.status
    row.dispatched_at = utcnow()
    row.status = "queued"
    session.flush()
    write_journal(
        session,
        kind="print_dispatched",
        actor="auto",
        qty=row.units,
        article_id=row.article_id,
        details={
            "dispatch_id": row.id,
            "item_id": item.id,
            "batch_id": row.bambuddy_batch_id,
        },
    )
    apply_outcome(session, row.id, classify(item), item.status)


def return_print(
    session: Session, dispatch_id: int, reason: str, *, rejected: bool = False
) -> None:
    """Put a print that is certainly not in Bambuddy back where it came from (decision 4).

    Allowed for a print never sent, or - with `rejected=True` - for a `sending` one whose
    `enqueue` Bambuddy rejected (4xx). A sent `sending` print without the flag may come
    from a crash or cancel after the mark (outcome unknown), so it is refused like an
    `unclear` or later one: putting it back could print it twice.
    """
    row = _dispatch(session, dispatch_id)
    never_sent = row.status in ("sending", "unclear") and row.enqueue_sent_at is None
    refused_after_send = rejected and row.status == "sending"
    if not (never_sent or refused_after_send):
        raise ValueError(f"dispatch {dispatch_id} cannot be returned ({row.status})")
    row.status = "returned"
    row.message = reason
    row.finished_at = utcnow()
    entry = None
    if row.queue_entry_id is not None:
        entry = session.get(WorkQueueEntry, row.queue_entry_id)
        # SQLite may have given the id of a deleted entry to a new one.
        if entry is not None and (
            entry.article_id != row.article_id or entry.origin != row.origin
        ):
            entry = None
    if entry is not None:
        entry.prints += 1
        session.flush()
    else:
        entry = _recreate_entry(session, row)
    write_journal(
        session,
        kind="print_returned",
        actor="auto",
        qty=row.units,
        article_id=row.article_id,
        details={"dispatch_id": row.id, "entry_id": entry.id, "reason": reason},
    )


def _recreate_entry(session: Session, row: Dispatch) -> WorkQueueEntry:
    """A new one-print entry at the old place: before every entry that was behind it."""
    entries = _ordered(session)
    index = sum(1 for e in entries if e.position < row.queue_position)
    entry = WorkQueueEntry(
        article_id=row.article_id, prints=1, origin=row.origin, position=index, held=False
    )
    session.add(entry)
    session.flush()
    entries.insert(index, entry)
    _renumber(session, entries)
    return entry


# ---- the hand-over ---------------------------------------------------------------------


def _put_back(
    sm: sessionmaker, res: Reservation, reason: str, *, rejected: bool = False
) -> None:
    with sm.begin() as session:
        return_print(session, res.dispatch_id, reason, rejected=rejected)


async def hand_over(
    client: BambuddyClient, sm: sessionmaker, res: Reservation, *, insert_next: bool
) -> HandOverResult:
    """Hand the reserved print to Bambuddy. No Bambuddy call runs inside a transaction."""
    try:
        batch_id = await client.ensure_batch(res.batch_name, res.external_ref, res.library_file_id)
        with sm.begin() as session:
            _dispatch(session, res.dispatch_id).bambuddy_batch_id = batch_id
        insert_at_top, insert_position = False, None
        if insert_next:
            queue = await client.list_queue()
            with sm() as session:
                own = own_item_ids(session)
            insert_at_top, insert_position = next_insert_position(queue, own)
    except BambuddyRejected as exc:
        _put_back(sm, res, str(exc)[:MAX_MESSAGE])
        return "rejected"
    except BambuddyUnavailable:
        _put_back(sm, res, "not_sent")
        return "not_sent"

    with sm.begin() as session:
        mark_sent(session, res.dispatch_id)
    request = EnqueueRequest(
        res.library_file_id,
        res.plate_id,
        res.target_model,
        res.printer_id,
        batch_id,
        insert_at_top,
        insert_position,
    )
    try:
        item = await client.enqueue(request)
    except BambuddyRejected as exc:
        _put_back(sm, res, str(exc)[:MAX_MESSAGE], rejected=True)
        return "rejected"
    except BambuddyUnavailable:
        with sm.begin() as session:
            mark_unclear(session, res.dispatch_id)
        return "unclear"
    with sm.begin() as session:
        confirm_dispatch(session, res.dispatch_id, item)
    return "queued"
