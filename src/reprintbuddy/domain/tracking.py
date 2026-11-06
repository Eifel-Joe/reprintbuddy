"""Following handed-over prints in Bambuddy and applying their outcome (Spec 7.5-7.7, 8.1).

Plan B4 decisions 3, 6, 7, 15, 18. Bambuddy's raw queue states are read in one place
only, `classify` (assumption A2). A state it does not know changes nothing: the print
stays as it is and the poll reports the raw text, so a warning appears (Spec 7.7).

`dispatch` imports this module, never the other way round.
The session functions never commit: the caller opens the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import QueueItem
from reprintbuddy.domain.inspection import open_inspection
from reprintbuddy.domain.pauses import pause_article
from reprintbuddy.domain.stock import write_journal
from reprintbuddy.domain.workqueue import OPEN_STATES, add_replacement
from reprintbuddy.models import Article, Dispatch, utcnow

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
# `dispatch.resolve_pending` (decision 5), which looks them up by batch.
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
        if row.status == state and row.last_status == raw_status:
            return False
        row.status = state
        row.last_status = raw_status
        session.flush()
        return True

    if raw_status is not None:
        row.last_status = raw_status
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


def _failed(session: Session, row: Dispatch, article: Article) -> None:
    """Spec 7.5, 7.7: a replacement goes to the front; the second failure in a row pauses."""
    row.status = "failed"
    article.consecutive_failures += 1
    session.flush()
    _journal(session, "print_failed", row)
    add_replacement(
        session, row.article_id, 1, "failure", "auto", details={"dispatch_id": row.id}
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
