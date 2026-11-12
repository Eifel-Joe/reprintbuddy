"""Following handed-over prints in Bambuddy and applying their outcome (Spec 7.5-7.7, 8.1).

Plan B4 decisions 3, 6, 7, 15, 18. Bambuddy's raw queue states are read in one place
only, `classify` (assumption A2). A state it does not know changes nothing: the print
stays as it is and the poll reports the raw text, so a warning appears (Spec 7.7).

`dispatch` imports this module, never the other way round.
The session functions never commit: the caller opens the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import Printer, QueueItem
from reprintbuddy.domain.inspection import open_inspection
from reprintbuddy.domain import articlesets
from reprintbuddy.domain.pauses import pause_article
from reprintbuddy.domain.stock import write_journal
from reprintbuddy.domain.workqueue import OPEN_STATES, add_replacement
from reprintbuddy.models import Article, Dispatch, Withdrawal, utcnow

Outcome = Literal["waiting", "printing", "completed", "failed", "cancelled", "missing", "unknown"]

STATUS_MAP: dict[str, Outcome] = {
    "pending": "waiting",
    "printing": "printing",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "skipped": "cancelled",
}

# Prints the poll asks Bambuddy about. `sending`/`unclear` are settled by
# `dispatch.resolve_pending` (decision 5), which looks for their entry among Bambuddy's
# entries by own user, the file and plate the print was sent with, and time (Spec 7.6).
POLLED_STATES = ("queued", "printing")
FAILURES_TO_PAUSE = 2


def classify(item: QueueItem | None) -> Outcome:
    """Bambuddy's raw state as an outcome; `None` (deleted in Bambuddy) is `missing`."""
    if item is None:
        return "missing"
    return STATUS_MAP.get(item.status, "unknown")


def apply_outcome(
    session: Session, dispatch_id: int, outcome: Outcome, raw_status: str | None
) -> bool:
    """Apply Bambuddy's answer to an open print; True when something changed.

    A finished print, an unknown outcome or an answer that matches what is already
    stored changes nothing (False). Every change keeps Bambuddy's raw state in
    `last_status`; `missing` has none, so the last state Bambuddy gave stays visible.
    """
    row = session.get(Dispatch, dispatch_id)
    if row is None:
        raise ValueError(f"unknown dispatch {dispatch_id}")
    if row.status not in OPEN_STATES or outcome == "unknown":
        return False
    if outcome in ("waiting", "printing"):
        state = "queued" if outcome == "waiting" else "printing"
        if row.withdraw_sent_at is not None:
            # Still there after a delete was sent: it did not take (Spec 7.2a). Without the mark a
            # later cancel or delete in Bambuddy is somebody else's again, with its pause (review 9).
            row.withdraw_sent_at = None
            session.flush()
        if row.status == state and row.last_status == raw_status:
            return False
        row.status = state
        row.last_status = raw_status
        session.flush()
        return True

    if raw_status is not None:
        row.last_status = raw_status
    if outcome in ("cancelled", "missing") and row.withdraw_sent_at is not None:
        # Reprintbuddy's own delete after a cancel (Spec 7.2a), its answer lost: gone (or kept by
        # Bambuddy as cancelled, #2960) because of it. Not a delete by somebody else: no pause (7.6).
        withdrawn(session, row.id)
        return True
    row.finished_at = utcnow()
    article = session.get(Article, row.article_id)
    if outcome == "completed":
        row.status = "completed"
        article.consecutive_failures = 0
        session.flush()
        open_inspection(session, row)
    elif outcome == "failed":
        _failed(session, row, article)
    elif outcome == "cancelled":
        row.status = "cancelled"
        session.flush()
        _journal(session, "print_cancelled", row, status=raw_status)
        pause_article(
            session, row.article_id, "cancelled_in_bambuddy", "auto",
            details={"dispatch_id": row.id},
        )
    elif outcome == "missing":
        row.status = "missing"
        session.flush()
        _journal(session, "print_missing", row)
        pause_article(
            session, row.article_id, "missing_in_bambuddy", "auto",
            details={"dispatch_id": row.id},
        )
    else:  # pragma: no cover - Outcome is closed
        raise ValueError(f"unknown outcome {outcome!r}")
    return True


def withdrawn(session: Session, dispatch_id: int, *, journal: bool = True) -> None:
    """A print Reprintbuddy deleted from Bambuddy after a cancel (Spec 7.2a): finished as `withdrawn`,
    no longer "in progress", one print fewer to take back for its article. Never a pause.

    `journal=False`: the engine's run writes one line per article for all it took back.
    """
    row = session.get(Dispatch, dispatch_id)
    if row is None:
        raise ValueError(f"unknown dispatch {dispatch_id}")
    row.status = "withdrawn"
    row.withdraw_sent_at = None
    row.finished_at = utcnow()
    note = session.get(Withdrawal, row.article_id)
    order_id = None if note is None else note.order_id
    if note is not None:
        note.prints -= 1
        if note.prints <= 0:
            session.delete(note)  # all noted prints are back
    session.flush()
    if journal:
        write_journal(
            session, kind="print_withdrawn", actor="auto", qty=1, article_id=row.article_id,
            order_id=order_id,
            details={"item_ids": [row.bambuddy_item_id], "dispatch_ids": [row.id]},
        )


def _failed(session: Session, row: Dispatch, article: Article) -> None:
    """Spec 7.5, 7.7: a replacement goes to the front; the second failure in a row pauses."""
    row.status = "failed"
    article.consecutive_failures += 1
    session.flush()
    _journal(session, "print_failed", row)
    add_replacement(
        session, row.article_id, 1, "failure", "auto",
        print_origin=row.origin, units=row.units, details={"dispatch_id": row.id},
    )
    if article.consecutive_failures >= FAILURES_TO_PAUSE:
        pause_article(
            session, row.article_id, "two_failures", "auto", details={"dispatch_id": row.id}
        )


def _journal(session: Session, kind: str, row: Dispatch, **extra: object) -> None:
    write_journal(
        session,
        kind=kind,
        actor="auto",
        qty=row.units,
        article_id=row.article_id,
        details={"dispatch_id": row.id, "item_id": row.bambuddy_item_id, **extra},
    )


# ---- the poll --------------------------------------------------------------------------


@dataclass
class PollReport:
    reachable: bool
    changed: int
    unexpected: list[str]  # raw states `classify` does not know, each once
    waiting_review: list[int]  # Bambuddy ids of own waiting entries with manual start
    error: str | None  # Bambuddy's refusal (4xx), decision 15


async def poll_dispatches(client: BambuddyClient, sm: sessionmaker) -> PollReport:
    """Ask Bambuddy about every `queued`/`printing` print, oldest first, and apply it.

    Only own entries are asked, by their stored id. Each answer is applied in its own
    transaction, never with a network call inside. Unreachable or refused: stop there;
    what was applied before stays, nothing else changes.
    """
    report = PollReport(reachable=True, changed=0, unexpected=[], waiting_review=[], error=None)
    with sm() as session:
        prints = session.execute(
            select(Dispatch.id, Dispatch.bambuddy_item_id)
            .where(Dispatch.status.in_(POLLED_STATES), Dispatch.bambuddy_item_id.is_not(None))
            .order_by(Dispatch.id)
        ).all()
    for dispatch_id, item_id in prints:
        try:
            item = await client.get_queue_item(item_id)
        except BambuddyUnavailable:
            report.reachable = False
            break
        except BambuddyRejected as exc:
            report.error = str(exc)
            break
        outcome = classify(item)
        if outcome == "unknown":
            if item.status not in report.unexpected:
                report.unexpected.append(item.status)
            continue
        if outcome == "waiting" and item.manual_start:
            report.waiting_review.append(item.id)
        with sm.begin() as session:
            if apply_outcome(session, dispatch_id, outcome, item.status if item else None):
                report.changed += 1
    return report


# ---- print file and print target -------------------------------------------------------


@dataclass(frozen=True)
class ArticleCheck:
    error: str | None  # "unavailable" or Bambuddy's refusal; then nothing was changed
    # Articles whose target is only temporarily unavailable (printer in maintenance mode);
    # they are not paused, the engine skips them when handing over (assumption A9).
    waiting_for_printer: frozenset[int]
    # Bambuddy's printer list as the check read it; the print plan uses it, so a regular query asks
    # for the printers once (0.4.2). Empty when the check failed. Not part of the comparison.
    printers: tuple[Printer, ...] = field(default=(), compare=False)


async def check_articles(client: BambuddyClient, sm: sessionmaker) -> ArticleCheck:
    """Spec 5.1, 7.7, decisions 9, 10: pause active articles whose file changed or vanished
    or whose print target is gone.

    Reads in a short session, asks Bambuddy, then writes each pause in its own transaction.
    Unreachable or refused: nothing is changed. A printer in maintenance mode (inactive) is
    only unavailable for now; a printer missing from the list counts as deleted.
    """
    with sm() as session:
        rows = session.execute(
            select(
                Article.id, Article.library_file_id, Article.file_name,
                Article.file_fingerprint, Article.target_model, Article.printer_id,
                Article.paused,
            )
            .where(articlesets.where_print())  # only print articles have a file and a target (0.4.0)
            .order_by(Article.id)
        ).all()
    try:
        printers = await client.list_printers()
        files = {}
        for file_id in sorted({row.library_file_id for row in rows}):
            files[file_id] = await client.get_file(file_id)
    except BambuddyUnavailable:
        return ArticleCheck("unavailable", frozenset())
    except BambuddyRejected as exc:
        return ArticleCheck(str(exc), frozenset())

    by_id = {printer.id: printer for printer in printers}
    waiting: set[int] = set()
    for row in rows:
        file = files[row.library_file_id]
        verdicts: list[tuple[str, dict]] = []
        if file is None:
            verdicts.append(("file_missing", {"file_name": row.file_name}))
        elif file.fingerprint != row.file_fingerprint or file.filename != row.file_name:
            verdicts.append((
                "file_changed",
                {
                    "old_fingerprint": row.file_fingerprint,
                    "new_fingerprint": file.fingerprint,
                    "old_name": row.file_name,
                    "new_name": file.filename,
                },
            ))
        if row.printer_id is not None:
            chosen = by_id.get(row.printer_id)
            target = {"printer_id": row.printer_id}
            matching = [chosen] if chosen else []
        elif row.target_model:
            target = {"target_model": row.target_model}
            matching = [p for p in printers if p.model == row.target_model]
        else:
            target, matching = {}, []
        if not matching:
            verdicts.append(("no_target", target))
        elif not any(p.active for p in matching) and not verdicts and not row.paused:
            waiting.add(row.id)
        for reason, details in verdicts:
            with sm.begin() as session:
                # The answer is minutes old by now and web requests run without the tick
                # lock: a confirm, relink, target change or archive in between wins.
                current = session.get(Article, row.id)
                if current is None or (
                    current.archived_at,
                    current.library_file_id,
                    current.file_fingerprint,
                    current.file_name,
                    current.printer_id,
                    current.target_model,
                ) != (
                    None,
                    row.library_file_id,
                    row.file_fingerprint,
                    row.file_name,
                    row.printer_id,
                    row.target_model,
                ):
                    break
                pause_article(session, row.id, reason, "auto", details=details)
    return ArticleCheck(None, frozenset(waiting), tuple(printers))
