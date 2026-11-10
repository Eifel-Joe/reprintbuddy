"""Handing prints from Reprintbuddy's work queue over to Bambuddy (Spec 7.3, 7.7).

Plan B4 decisions 1-5: one print at a time, each as a single entry in Bambuddy's queue
(Spec 7.6, since v0.3.5; before, each print had its own batch).

1. `reserve_next` takes one print off the top eligible entry in a short write
   transaction and creates the `dispatches` row in state `sending`, with the article's
   file and plate as they are now (v0.3.11 p53).
2. `hand_over` calls Bambuddy outside any transaction: optionally `list_queue`, then -
   after `enqueue_sent_at` has been committed - `enqueue` with exactly that file and plate
   (the `Reservation` is read back from the row, one source).

The order in step 2 is the safety of the whole automation: once `enqueue_sent_at` is
set, nothing here puts the print back into the work queue except a 4xx answer
(Bambuddy said for certain it did not take it). An unclear answer leaves the print
`unclear`; the tick looks for it in Bambuddy and never sends it again (decision 5).
Any other exception after the mark leaves it `sending` with the mark set, which the
tick treats the same way.

The session functions never commit: the caller opens the transaction.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy import settings
from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import EnqueueRequest, QueueItem
from reprintbuddy.domain.pauses import pause_article
from reprintbuddy.domain.stock import write_journal
from reprintbuddy.domain.tracking import apply_outcome, classify
from reprintbuddy.domain.workqueue import OPEN_STATES, _ordered, _renumber
from reprintbuddy.models import Article, Dispatch, JournalEntry, WorkQueueEntry, utcnow

HandOverResult = Literal["queued", "unclear", "not_sent", "rejected", "held_for_update"]
MAX_MESSAGE = 500


@dataclass(frozen=True)
class Reservation:
    dispatch_id: int
    article_id: int
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
        status="sending",
        units=article.units_per_print,
        queue_entry_id=entry.id,
        queue_position=entry.position,
        library_file_id=article.library_file_id,
        plate_id=article.plate_id,
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
    # File and plate from the row, not from the article a second time: what is sent to Bambuddy is
    # what the search after an unclear answer looks for (p53; the article may change in between).
    return Reservation(
        dispatch_id=row.id,
        article_id=article.id,
        library_file_id=row.library_file_id,
        plate_id=row.plate_id,
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


def foreign_ahead(rows: Sequence[tuple[QueueItem, bool]]) -> bool:
    """Is a print in Bambuddy's queue that Reprintbuddy did not make, and that runs or comes first?

    `rows` are the queue entries with the answer "own" (Spec 7.6: an id Reprintbuddy knows, or the
    user of the key). A foreign print runs now, or it waits and no own waiting entry lies behind it
    (with no own entry waiting at all, every foreign waiting print counts: an own print handed over
    now goes behind it, unless "insert next" puts it on top, see `next_insert_position`; the hint is
    then too careful, never too quiet). A foreign waiting print behind every own one does not
    (v0.3.16 p65). Positions of all printers are compared alike, as `next_insert_position` does.
    """
    waiting_own = [item for item, own in rows if own and item.status == "pending"]
    last_own = max((i.position for i in waiting_own if i.position is not None), default=None)
    for item, own in rows:
        if own:
            continue
        if item.status == "printing":
            return True
        if item.status == "pending":
            if not waiting_own or item.position is None or last_own is None or item.position < last_own:
                return True
    return False


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
        details={"dispatch_id": row.id},
    )


def confirm_dispatch(session: Session, dispatch_id: int, item: QueueItem) -> None:
    """Bambuddy answered `enqueue` with `item`: the print is handed over (see `adopt`)."""
    adopt(session, dispatch_id, item)


def adopt(session: Session, dispatch_id: int, item: QueueItem) -> None:
    """Bambuddy holds the print as `item`; its state follows `tracking.classify`.

    Used for `enqueue`'s answer and for the one entry found after an unclear hand-over
    (decision 5, Spec 7.6). The print becomes `queued` and is handed over (journal,
    once per print), then Bambuddy's state is applied like a poll answer: `printing`,
    or - for an entry found already finished - provisional stock, replacement or pause.
    An unknown state leaves it `queued`; the next poll reports it.
    """
    row = _dispatch(session, dispatch_id)
    if row.status not in ("sending", "unclear"):
        raise ValueError(f"dispatch {dispatch_id} cannot be adopted ({row.status})")
    row.bambuddy_item_id = item.id
    row.last_status = item.status
    row.dispatched_at = utcnow()
    row.status = "queued"
    session.flush()
    if not _dispatched_noted(session, row):
        write_journal(
            session,
            kind="print_dispatched",
            actor="auto",
            qty=row.units,
            article_id=row.article_id,
            details={"dispatch_id": row.id, "item_id": item.id},
        )
    apply_outcome(session, row.id, classify(item), item.status)


def _dispatched_noted(session: Session, row: Dispatch) -> bool:
    """Whether the journal already says this print was handed over."""
    lines = session.scalars(
        select(JournalEntry.details).where(
            JournalEntry.kind == "print_dispatched", JournalEntry.article_id == row.article_id
        )
    )
    return any((details or {}).get("dispatch_id") == row.id for details in lines)


def mark_lost(
    session: Session, dispatch_id: int, *, item_ids: Sequence[int] | None = None
) -> None:
    """A sent print without one clear entry in Bambuddy: `missing`, article paused `unclear`.

    Decision 5, Spec 7.6. Without `item_ids` Bambuddy has no entry for it; with them
    several entries could be it and none is adopted (reason `ambiguous`, the ids go into
    the journal and the pause).

    Not `tracking.apply_outcome(..., "missing")`: that is for an entry deleted in
    Bambuddy (pause `missing_in_bambuddy`). Here it is unknown whether Bambuddy ever
    took the print - or printed it and its history was cleared, or which entry it is - so
    a person has to look. The print is never sent again; it is finished and stays out of
    the work queue.
    """
    row = _dispatch(session, dispatch_id)
    if row.status not in ("sending", "unclear") or row.enqueue_sent_at is None:
        raise ValueError(f"dispatch {dispatch_id} cannot be lost ({row.status})")
    row.status = "missing"
    row.finished_at = utcnow()
    session.flush()
    write_journal(
        session,
        kind="print_missing",
        actor="auto",
        qty=row.units,
        article_id=row.article_id,
        details=(
            {"dispatch_id": row.id, "reason": "not_found_after_send"}
            if item_ids is None
            else {"dispatch_id": row.id, "reason": "ambiguous", "item_ids": list(item_ids)}
        ),
    )
    pause_details: dict[str, object] = {"dispatch_id": row.id}
    if item_ids is not None:
        pause_details["item_ids"] = list(item_ids)
    pause_article(session, row.article_id, "unclear", "auto", details=pause_details)


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
    client: BambuddyClient,
    sm: sessionmaker,
    res: Reservation,
    *,
    insert_next: bool,
    may_send: Callable[[], bool | Awaitable[bool]] = lambda: True,
) -> HandOverResult:
    """Hand the reserved print to Bambuddy. No Bambuddy call runs inside a transaction.

    `may_send` (plain or `async`) is asked right before the mark: False (an update has started) puts
    the never-sent print back and returns `held_for_update`. An update backs the database
    up while the service runs and restores it on failure; a print sent after the
    backup would be in Bambuddy but back in the restored work queue - printed twice.
    """
    insert_at_top, insert_position = False, None
    try:
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

    allowed = may_send()
    if inspect.isawaitable(allowed):
        allowed = await allowed
    if not allowed:
        _put_back(sm, res, "not_sent")
        return "held_for_update"
    with sm.begin() as session:
        mark_sent(session, res.dispatch_id)
    request = EnqueueRequest(
        res.library_file_id,
        res.plate_id,
        res.target_model,
        res.printer_id,
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


# ---- settling unclear hand-overs -------------------------------------------------------

# How much earlier than `enqueue_sent_at` an entry may be stamped and still count as made
# after the send (Spec 7.6). Root: `created_at` comes from Bambuddy's clock, `enqueue_sent_at`
# from Reprintbuddy's; on two machines they may differ by a little. Without the margin a
# Bambuddy clock running behind would make the own entry look older than the send, the print
# would count as lost and the article would pause for nothing. The price: an older own
# entry of the same file and plate that Reprintbuddy does not know and that lies within the
# margin becomes a second candidate, so the print is not adopted but paused (never guessed,
# never sent twice). Rejected: no margin (pauses on every slight clock drift); a larger one
# (more false "ambiguous"); an upper bound on `created_at` (the spec asks only for "made
# after the send", and a slow Bambuddy may create the entry long after the timeout).
CLOCK_TOLERANCE = timedelta(minutes=5)

# The warning text when the own Bambuddy user is unknown (`ResolveReport.error`). The engine
# only runs with a configured connection, which stores the user, so this is a safety net.
NO_OWN_USER = "the Bambuddy user of the key is not known; check the connection in the settings"


@dataclass
class ResolveReport:
    reachable: bool
    resolved: int  # sent prints found in Bambuddy and adopted
    unresolved: int  # sent prints left `unclear` because the search could not run
    duplicates: list[list[int]]  # Bambuddy ids per print that could not be told apart
    lost: list[int]  # dispatch ids of sent prints Bambuddy does not have
    error: str | None = None  # the search was refused or could not run (shown as a warning)
    # Bambuddy id -> article of the groups in `duplicates`: no print holds these ids, so the
    # engine keeps the warning by the article's pause (`Engine._prune_duplicates`).
    duplicate_articles: dict[int, int] = field(default_factory=dict)


def _known_item_ids(session: Session) -> set[int]:
    """Bambuddy ids held by any print in Reprintbuddy, settled ones included."""
    return set(
        session.scalars(
            select(Dispatch.bambuddy_item_id).where(Dispatch.bambuddy_item_id.is_not(None))
        )
    )


def _sent_file(row: Dispatch, article: Article | None) -> tuple[int, int] | None:
    """File and plate the print was handed over with.

    Stored at the reservation since revision 0010 (v0.3.11 p53), so a file chosen for the article
    while the print is unclear does not hide its entry. A print from before has none: the article's
    file and plate at the time of the search stand in, as in the versions before (a file changed
    meanwhile then makes the print `missing` and pauses the article - safe, only unneeded).
    """
    if row.library_file_id is not None and row.plate_id is not None:
        return row.library_file_id, row.plate_id
    if article is None:
        return None
    return article.library_file_id, article.plate_id


def _candidates(
    items: Sequence[QueueItem], *, username: str, sent_file: tuple[int, int] | None,
    sent_at: datetime, known: Collection[int],
) -> list[QueueItem]:
    """The entries that may be this print's (Spec 7.6), smallest id first.

    Own user, the file and plate the print was sent with (`_sent_file`), made after the send
    (less the clock tolerance) and held by no print yet. An entry without `created_at` cannot
    be placed in time, so it is none.
    """
    if sent_file is None:
        return []
    file_id, plate_id = sent_file
    not_before = sent_at - CLOCK_TOLERANCE
    return sorted(
        (
            item
            for item in items
            if item.created_by == username
            and item.library_file_id == file_id
            and item.plate_id == plate_id
            and item.created_at is not None
            and item.created_at >= not_before
            and item.id not in known
        ),
        key=lambda item: item.id,
    )


def _keep_unclear(sm: sessionmaker, dispatch_ids: Sequence[int]) -> None:
    with sm.begin() as session:
        for dispatch_id in dispatch_ids:
            mark_unclear(session, dispatch_id)


async def resolve_pending(client: BambuddyClient, sm: sessionmaker) -> ResolveReport:
    """Settle every print in `sending`/`unclear` (decision 5, Spec 7.6); never sends anything.

    1. Never sent (`enqueue_sent_at` empty): back to the work queue, without the network.
       Newest first, so prints reserved one after another land where they were.
    2. Sent: Bambuddy's whole queue is read once (`list_all_items`), then each print is
       matched against the entries no print holds (`_candidates`). One candidate is
       adopted; none: the print is lost (`mark_lost`); several: none is adopted, the print
       is `missing` and the article paused (`unclear`) with the ids - never a guess, never
       sent again.

    Oldest send first, each print with the rule of Spec 7.6 as it stands: an entry adopted
    for an older print is known and drops out of the later prints' windows. Normally at
    most one sent print is unclear at a time (the engine hands nothing over while one is).
    Should there be more of one article, an older print sees the later print's entry in its
    window too and pauses as ambiguous rather than guessing. Rejected: newest first - it
    gives each print of two sent minutes apart its own entry, but a print that never
    reached Bambuddy (crash after the mark) would take the entry of an older one.

    The search cannot run (unreachable, refused, own Bambuddy user unknown): every sent
    print stays (or becomes) `unclear` and counts as `unresolved`, so the engine hands
    nothing new over until it is clear. Every step is its own short transaction, and no
    Bambuddy call runs inside one.
    """
    report = ResolveReport(reachable=True, resolved=0, unresolved=0, duplicates=[], lost=[])
    with sm() as session:
        rows = session.execute(
            select(Dispatch.id, Dispatch.enqueue_sent_at)
            .where(Dispatch.status.in_(("sending", "unclear")))
            .order_by(Dispatch.id)
        ).all()
    never_sent = [d for d, sent_at in rows if sent_at is None]
    sent = sorted((sent_at, d) for d, sent_at in rows if sent_at is not None)

    for dispatch_id in reversed(never_sent):
        with sm.begin() as session:
            return_print(session, dispatch_id, "not_sent")
    if not sent:
        return report
    sent_ids = [d for _, d in sent]

    with sm() as session:
        username = settings.get_setting(session, "bambuddy_username")
    if not isinstance(username, str) or not username.strip():
        # Without the own user no entry can be told apart from someone else's: deciding
        # "lost" now would pause for nothing, adopting could take a foreign entry.
        _keep_unclear(sm, sent_ids)
        report.unresolved = len(sent_ids)
        report.error = NO_OWN_USER
        return report

    try:
        items = await client.list_all_items()
    except (BambuddyUnavailable, BambuddyRejected) as exc:
        report.reachable = not isinstance(exc, BambuddyUnavailable)
        if isinstance(exc, BambuddyRejected):
            report.error = str(exc)[:MAX_MESSAGE]
        _keep_unclear(sm, sent_ids)
        report.unresolved = len(sent_ids)
        return report

    for sent_at, dispatch_id in sent:
        with sm.begin() as session:
            row = _dispatch(session, dispatch_id)
            article = session.get(Article, row.article_id)
            # Read per print: an entry adopted a moment ago for an older print is known now.
            known = _known_item_ids(session)
            found = _candidates(
                items, username=username, sent_file=_sent_file(row, article), sent_at=sent_at,
                known=known,
            )
            if len(found) == 1:
                adopt(session, dispatch_id, found[0])
                report.resolved += 1
                continue
            if not found:
                mark_lost(session, dispatch_id)
                report.lost.append(dispatch_id)
                continue
            ids = [item.id for item in found]
            mark_lost(session, dispatch_id, item_ids=ids)
            # Always written down: the pause in `mark_lost` writes nothing when the article
            # is already paused, and the warning lives only in memory.
            write_journal(
                session,
                kind="print_duplicates",
                actor="auto",
                article_id=row.article_id,
                details={"dispatch_id": dispatch_id, "item_ids": ids},
            )
            report.duplicates.append(ids)
            report.duplicate_articles.update({item_id: row.article_id for item_id in ids})
    return report
