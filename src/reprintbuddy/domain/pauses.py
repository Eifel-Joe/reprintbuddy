"""Article pauses of the automation (Spec 5.1, plan B4 decision 8).

Functions never commit: the caller opens the transaction.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

from reprintbuddy.domain.errors import ConflictError, NotFoundError
from reprintbuddy.domain.stock import Actor, write_journal
from reprintbuddy.models import Article

PauseReason = Literal[
    "queue_reduced",
    "cancelled_in_bambuddy",
    "missing_in_bambuddy",
    "two_failures",
    "file_changed",
    "file_missing",
    "no_target",
    "unclear",
]

PAUSE_REASONS: frozenset[str] = frozenset(PauseReason.__args__)  # type: ignore[attr-defined]
FILE_REASONS = frozenset({"file_changed", "file_missing"})


def _article(session: Session, article_id: int) -> Article:
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    return article


def pause_article(
    session: Session,
    article_id: int,
    reason: PauseReason,
    actor: Actor,
    *,
    details: dict | None = None,
) -> bool:
    """Pause the article; True when something changed.

    The first reason stays, with one exception: a file reason replaces another reason,
    because the file has to be settled before the pause can be lifted (Spec 5.1).
    """
    article = _article(session, article_id)
    replaced: str | None = None
    if article.paused:
        old = article.pause_reason
        if reason not in FILE_REASONS or old == reason:
            return False
        replaced = old
    article.paused = True
    article.pause_reason = reason
    entry_details = {**(details or {}), "reason": reason}
    if replaced is not None:
        entry_details["replaced"] = replaced
    session.flush()
    write_journal(
        session,
        kind="automation_paused",
        actor=actor,
        article_id=article.id,
        details=entry_details,
    )
    return True


def lift_pause(session: Session, article_id: int, actor: Actor) -> None:
    """Lift the pause and reset the failure counter (decision 8)."""
    article = _article(session, article_id)
    if not article.paused:
        raise ConflictError("queue.not_paused")
    reason = article.pause_reason
    if reason in FILE_REASONS:
        raise ConflictError("articles.pause_needs_file")
    if reason == "no_target" and not (article.target_model or article.printer_id):
        raise ConflictError("articles.pause_needs_target")
    article.paused = False
    article.pause_reason = None
    article.consecutive_failures = 0
    session.flush()
    write_journal(
        session,
        kind="automation_resumed",
        actor=actor,
        article_id=article.id,
        details={"reason": reason},
    )
