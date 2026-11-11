"""Taking back reprints after a cancel (Spec 7.2a, 0.4.1 p382).

Prints are not tied to an order: rule 7.1 plans them from the coverage, and an order lowers the
coverage through "available". A cancel raises it again; what is then planned over the target comes
back. The surplus is counted per print article the order touched (its lines, the parts of an article
made of parts in them): coverage minus target, for a part with the demand from assembly (Spec 7.1).
Only whole prints, so the coverage never falls below the target.

At the cancel (`withdraw_after_cancel`, inside its transaction): rule entries of Reprintbuddy's queue
shrink from the bottom, held ones included. What is still over the target is noted per article in
`withdrawals`: that many of its waiting rule prints in Bambuddy, the newest first. The engine deletes
them on its runs (`withdraw_from_bambuddy`), never inside a transaction, recounting the surplus each
time. Prints of another origin (replacement, by hand), prints on their way (`sending`, `unclear`) and
started or finished ones are never taken back. Nothing here pauses an article (Spec 7.2, 7.6, 7.7):
the journal says what came back, with the order.

Rejected: tying prints to orders (Spec 7.1 plans per article; a cancel of one of two orders would
need a guess which print was whose); taking back on every drop of the demand (Spec 13: only the
cancel, decided 11.11.2026); `workqueue.set_prints`/`delete_entry` for shrinking (they pause, Spec 7.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected, BambuddyUnavailable
from reprintbuddy.domain import articlesets, tracking
from reprintbuddy.domain.stock import write_journal
from reprintbuddy.domain.workqueue import OPEN_STATES, _ordered, _renumber, coverage_of
from reprintbuddy.models import Article, Dispatch, Order, OrderLine, Withdrawal, WorkQueueEntry, utcnow

MAX_MESSAGE = 500


@dataclass(frozen=True)
class WithdrawReport:
    queue_prints: int  # taken out of Reprintbuddy's queue at the cancel
    bambuddy_prints: int  # noted for Bambuddy: waiting prints the engine deletes next


def affected_articles(session: Session, order_id: int) -> list[int]:
    """Active print articles whose demand the order made: its lines (articles, the articles of a set,
    the parts of an article made of parts built on order), and for an article made of parts built on
    stock (its variant or itself) the parts, whose demand from assembly it raises (Spec 7.1)."""
    from reprintbuddy.domain import assembled  # assembled builds on workqueue, as this module does

    found: set[int] = set()
    line_articles = session.scalars(
        select(OrderLine.article_id).where(OrderLine.order_id == order_id).distinct()
    ).all()
    for article_id in line_articles:
        article = session.get(Article, article_id)
        if article is None:
            continue
        if article.kind == articlesets.PRINT:
            found.add(article.id)
        else:
            found.update(assembled.needs_of(session, article))
    if not found:
        return []
    return list(session.scalars(
        select(Article.id).where(Article.id.in_(found), articlesets.where_print()).order_by(Article.id)
    ))


def surplus_units(session: Session, article: Article, demand: Mapping[int, int]) -> int:
    """Pieces planned over the target: coverage - (minimum + demand from assembly + buffer). The
    target holds without a minimum too (then it is the buffer). May be negative."""
    target = article.min_stock + demand.get(article.id, 0) + article.buffer
    return coverage_of(session, article).total - target


def bambuddy_candidates(session: Session, article_id: int) -> list[Dispatch]:
    """Rule prints Bambuddy keeps waiting, as far as Reprintbuddy knows, and whose delete is not under
    way: the newest first (they would print last)."""
    return list(session.scalars(
        select(Dispatch)
        .where(
            Dispatch.article_id == article_id,
            Dispatch.origin == "rule",
            Dispatch.status == "queued",
            Dispatch.bambuddy_item_id.is_not(None),
            Dispatch.withdraw_sent_at.is_(None),
        )
        .order_by(Dispatch.dispatched_at.desc().nulls_last(), Dispatch.id.desc())
    ))


def _shrink_queue(session: Session, article: Article, surplus: int) -> tuple[int, list[int], int]:
    """Take whole prints off the article's rule entries, from the bottom; (prints, entry ids, rest)."""
    taken, touched = 0, []
    entries = session.scalars(
        select(WorkQueueEntry)
        .where(WorkQueueEntry.article_id == article.id, WorkQueueEntry.origin == "rule")
        .order_by(WorkQueueEntry.position.desc(), WorkQueueEntry.id.desc())
    ).all()
    for entry in entries:
        if surplus < article.units_per_print:
            break
        n = min(entry.prints, surplus // article.units_per_print)
        surplus -= n * article.units_per_print
        taken += n
        touched.append(entry.id)
        if n == entry.prints:
            session.delete(entry)
        else:
            entry.prints -= n
    if touched:
        session.flush()
        _renumber(session, _ordered(session))
    return taken, touched, surplus


def countable(candidates: list[Dispatch], surplus: int) -> list[Dispatch]:
    """The candidates that fit into the surplus, in order: a print is taken only while all its pieces
    are over the target."""
    chosen = []
    for row in candidates:
        if surplus >= row.units:
            chosen.append(row)
            surplus -= row.units
    return chosen


def _note(session: Session, article_id: int, prints: int, order_id: int) -> None:
    """The note is the surplus now, not added to an older one (a second cancel recounts it)."""
    row = session.get(Withdrawal, article_id)
    if prints <= 0:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        row = Withdrawal(article_id=article_id)
        session.add(row)
    row.prints, row.order_id, row.refused, row.unreached = prints, order_id, None, False


def withdraw_after_cancel(session: Session, order_id: int) -> WithdrawReport:
    """Spec 7.2a, right after the reservations of the order were released (same transaction)."""
    from reprintbuddy.domain import assembled

    session.flush()
    demand = assembled.assembly_demand(session)
    queue_total = bambuddy_total = 0
    for article_id in affected_articles(session, order_id):
        article = session.get(Article, article_id)
        surplus = surplus_units(session, article, demand)
        taken, entries, surplus = _shrink_queue(session, article, surplus)
        if taken:
            write_journal(
                session, kind="queue_entry_withdrawn", actor="auto", qty=taken, article_id=article_id,
                order_id=order_id, details={"entries": entries},
            )
        noted = len(countable(bambuddy_candidates(session, article_id), surplus))
        _note(session, article_id, noted, order_id)
        queue_total += taken
        bambuddy_total += noted
    session.flush()
    return WithdrawReport(queue_total, bambuddy_total)


def preview_cancel(sm: sessionmaker, order_id: int) -> WithdrawReport:
    """What a cancel of the open order would take back, for the confirmation page (Spec 7.2a).

    The cancel runs in a session of its own that is rolled back and never committed: the change feed
    and the activity log publish only on a commit, so nothing leaves it (no live event, no push).
    """
    from reprintbuddy.domain import orders  # orders calls this module

    with sm() as session:
        try:
            order = session.get(Order, order_id)
            if order is None or order.status != "open":
                return WithdrawReport(0, 0)
            return orders.cancel_order(session, order_id, "user")
        finally:
            session.rollback()


# ---- the engine's run: deleting the noted prints from Bambuddy -----------------------------------


@dataclass
class WithdrawRun:
    reachable: bool = True
    withdrawn: int = 0


def has_work(sm: sessionmaker) -> bool:
    """A note to work on, or a print whose delete was sent and not settled."""
    with sm() as session:
        if session.scalar(select(Withdrawal.article_id).limit(1)) is not None:
            return True
        return session.scalar(
            select(Dispatch.id).where(Dispatch.withdraw_sent_at.is_not(None), Dispatch.status.in_(OPEN_STATES)).limit(1)
        ) is not None


def mark_unreached(sm: sessionmaker) -> None:
    """A run could not reach Bambuddy: open notes say so on the overview (A2, Spec 7.2a)."""
    with sm.begin() as session:
        for note in session.scalars(select(Withdrawal).where(Withdrawal.prints > 0, Withdrawal.refused.is_(None))):
            note.unreached = True


async def _settle_marked(client: BambuddyClient, sm: sessionmaker) -> bool:
    """Prints whose delete was sent without a clear answer: ask Bambuddy. Gone (or cancelled) is
    withdrawn; anything else loses the mark and takes its state. False when Bambuddy did not answer."""
    with sm() as session:
        marked = session.execute(
            select(Dispatch.id, Dispatch.bambuddy_item_id)
            .where(Dispatch.withdraw_sent_at.is_not(None), Dispatch.status.in_(OPEN_STATES))
            .order_by(Dispatch.id)
        ).all()
    for dispatch_id, item_id in marked:
        try:
            item = await client.get_queue_item(item_id)
        except BambuddyUnavailable:
            return False
        except BambuddyRejected:
            return True  # the poll reports a refused read; the mark stays for the next run
        outcome = tracking.classify(item)
        with sm.begin() as session:
            if outcome not in ("missing", "cancelled"):
                session.get(Dispatch, dispatch_id).withdraw_sent_at = None
            tracking.apply_outcome(session, dispatch_id, outcome, item.status if item else None)
    return True


def _drop_or_pick(session: Session, note: Withdrawal) -> list[tuple[int, int]]:
    """The prints to delete now, as (dispatch id, item id): at most `note.prints`, fitting into the
    surplus as it is now. Drops the note when no whole print is over the target any more, or when the
    article has no open rule print left that could still come back (Spec 7.2a)."""
    from reprintbuddy.domain import assembled

    article = session.get(Article, note.article_id)
    surplus = -1
    if article is not None and article.archived_at is None:
        surplus = surplus_units(session, article, assembled.assembly_demand(session))
    open_rule = session.scalar(
        select(Dispatch.id).where(
            Dispatch.article_id == note.article_id, Dispatch.origin == "rule", Dispatch.status.in_(OPEN_STATES)
        ).limit(1)
    )
    if note.prints <= 0 or article is None or surplus < article.units_per_print or open_rule is None:
        session.delete(note)
        return []
    note.unreached = False
    if note.refused is not None:
        return []  # refused: no further try (Spec 7.2a); kept for the overview while prints wait
    chosen = countable(bambuddy_candidates(session, note.article_id), surplus)[: note.prints]
    return [(row.id, row.bambuddy_item_id) for row in chosen]


async def withdraw_from_bambuddy(client: BambuddyClient, sm: sessionmaker) -> WithdrawRun:
    """Delete the noted prints (Spec 7.2a). One print at a time, never with a network call inside a
    transaction: mark it (`withdraw_sent_at`, committed), delete, then settle in a new transaction.

    - deleted, kept as cancelled (#2960) or already gone: withdrawn, no pause;
    - 400 (it started meanwhile): the mark goes, its state is read again and applied; the note stays
      while the surplus does, so a print that falls back to waiting is taken on a later run;
    - another refusal (403: the right, or not an own entry): journal line, the note keeps Bambuddy's
      answer for the overview, no further try for this article;
    - no answer: the mark stays (settled on the next run by asking), the run ends unreachable.
    """
    run = WithdrawRun()
    if not await _settle_marked(client, sm):
        run.reachable = False
        mark_unreached(sm)
        return run
    with sm() as session:
        article_ids = list(session.scalars(select(Withdrawal.article_id).order_by(Withdrawal.article_id)))
    for article_id in article_ids:
        with sm.begin() as session:
            note = session.get(Withdrawal, article_id)
            order_id = None if note is None else note.order_id
            picked = [] if note is None else _drop_or_pick(session, note)
        items: list[int] = []
        ids: list[int] = []
        for dispatch_id, item_id in picked:
            with sm.begin() as session:
                session.get(Dispatch, dispatch_id).withdraw_sent_at = utcnow()
            try:
                await client.delete_queue_item(item_id)
            except BambuddyUnavailable:
                run.reachable = False
                break
            except BambuddyRejected as exc:
                if await _refused(client, sm, article_id, order_id, dispatch_id, item_id, exc):
                    continue
                break
            with sm.begin() as session:
                tracking.withdrawn(session, dispatch_id, journal=False)
            items.append(item_id)
            ids.append(dispatch_id)
            run.withdrawn += 1
        with sm.begin() as session:
            if items:
                write_journal(
                    session, kind="print_withdrawn", actor="auto", qty=len(items), article_id=article_id,
                    order_id=order_id, details={"item_ids": items, "dispatch_ids": ids},
                )
            note = session.get(Withdrawal, article_id)
            if note is not None and run.reachable:
                _drop_or_pick(session, note)  # once more: a note with no surplus left goes now
        if not run.reachable:
            mark_unreached(sm)
            return run
    return run


async def _refused(
    client: BambuddyClient, sm: sessionmaker, article_id: int, order_id: int | None,
    dispatch_id: int, item_id: int, exc: BambuddyRejected,
) -> bool:
    """Bambuddy refused one delete. True: it started meanwhile, go on with the next print."""
    with sm.begin() as session:
        session.get(Dispatch, dispatch_id).withdraw_sent_at = None
    if exc.status == 400:
        try:
            item = await client.get_queue_item(item_id)
        except (BambuddyUnavailable, BambuddyRejected):
            return True  # the poll reads it later; without the mark it waits there as before
        outcome = tracking.classify(item)
        if outcome != "waiting":
            with sm.begin() as session:
                tracking.apply_outcome(session, dispatch_id, outcome, item.status if item else None)
            return True
    message = (exc.detail or str(exc))[:MAX_MESSAGE]
    with sm.begin() as session:
        note = session.get(Withdrawal, article_id)
        prints = note.prints if note is not None else 0
        if note is not None:
            note.refused = message
        write_journal(
            session, kind="print_withdraw_refused", actor="auto", qty=prints, article_id=article_id,
            order_id=order_id, details={"item_id": item_id, "status": exc.status, "message": message},
        )
    return False
