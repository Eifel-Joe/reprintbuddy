"""Entries of Reprintbuddy's own work queue (Spec 7.2, 10, plan B4 decision 11).

Positions are integers, smaller = earlier; after every change they are renumbered
0 to n-1. Moving writes no journal line. Functions never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain.errors import NotFoundError, ValidationError
from reprintbuddy.domain.pauses import pause_article
from reprintbuddy.domain.stock import Actor, write_journal
from reprintbuddy.models import Article, WorkQueueEntry

Origin = Literal["rule", "scrap", "failure", "manual"]
MAX_PRINTS = 1000


@dataclass(frozen=True)
class EntryRow:
    id: int
    article_id: int
    article_name: str
    prints: int
    origin: str
    held: bool
    article_paused: bool
    created_at: datetime


def _ordered(session: Session) -> list[WorkQueueEntry]:
    return list(
        session.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position, WorkQueueEntry.id))
    )


def _renumber(session: Session, entries: list[WorkQueueEntry]) -> None:
    for index, entry in enumerate(entries):
        if entry.position != index:
            entry.position = index
    session.flush()


def _entry(session: Session, entry_id: int) -> WorkQueueEntry:
    entry = session.get(WorkQueueEntry, entry_id)
    if entry is None:
        raise NotFoundError("queue.entry_not_found")
    return entry


def _prints(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_PRINTS
    ):
        raise ValidationError(
            "form.invalid_number", field="queue.field.prints", min=1, max=MAX_PRINTS
        )
    return value


def list_entries(session: Session) -> list[EntryRow]:
    rows = session.execute(
        select(WorkQueueEntry, Article)
        .join(Article, Article.id == WorkQueueEntry.article_id)
        .order_by(WorkQueueEntry.position, WorkQueueEntry.id)
    ).all()
    return [
        EntryRow(
            id=entry.id,
            article_id=article.id,
            article_name=article.name,
            prints=entry.prints,
            origin=entry.origin,
            held=entry.held,
            article_paused=article.paused,
            created_at=entry.created_at,
        )
        for entry, article in rows
    ]


def add_entry(
    session: Session,
    article_id: int,
    prints: int,
    origin: Origin,
    actor: Actor,
    *,
    where: Literal["start", "end"] | int = "end",
    details: dict | None = None,
) -> WorkQueueEntry:
    """New entry at the start, at the end, or before the entry with id `where`."""
    prints = _prints(prints)
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    if article.archived_at is not None:
        raise ValidationError("queue.article_archived", name=article.name)
    entries = _ordered(session)
    if where == "end":
        index = len(entries)
    elif where == "start":
        index = 0
    else:
        ids = [e.id for e in entries]
        if where not in ids:
            raise NotFoundError("queue.entry_not_found")
        index = ids.index(where)
    entry = WorkQueueEntry(
        article_id=article_id, prints=prints, origin=origin, position=index, held=False
    )
    session.add(entry)
    session.flush()
    entries.insert(index, entry)
    _renumber(session, entries)
    write_journal(
        session,
        kind="queue_entry_created",
        actor=actor,
        qty=prints,
        article_id=article_id,
        details={"entry_id": entry.id, "origin": origin, **(details or {})},
    )
    return entry


def add_replacement(
    session: Session,
    article_id: int,
    prints: int,
    origin: Literal["scrap", "failure"],
    actor: Actor,
    *,
    details: dict | None = None,
) -> WorkQueueEntry:
    """A replacement print goes to the front of the queue."""
    return add_entry(session, article_id, prints, origin, actor, where="start", details=details)


def move_entry(session: Session, entry_id: int, to: Literal["top", "bottom"] | int) -> None:
    """Move an entry to the top, to the bottom, or before the entry with id `to`."""
    entry = _entry(session, entry_id)
    target = None if isinstance(to, str) else _entry(session, to)
    if target is entry:
        return
    entries = [e for e in _ordered(session) if e.id != entry.id]
    if to == "top":
        index = 0
    elif to == "bottom":
        index = len(entries)
    else:
        index = entries.index(target)
    entries.insert(index, entry)
    _renumber(session, entries)


def hold_entry(session: Session, entry_id: int, actor: Actor) -> None:
    entry = _entry(session, entry_id)
    if entry.held:
        return
    entry.held = True
    session.flush()
    write_journal(
        session,
        kind="queue_entry_held",
        actor=actor,
        qty=entry.prints,
        article_id=entry.article_id,
        details={"entry_id": entry.id},
    )


def release_entry(session: Session, entry_id: int, actor: Actor) -> None:
    entry = _entry(session, entry_id)
    if not entry.held:
        return
    entry.held = False
    session.flush()
    write_journal(
        session,
        kind="queue_entry_released",
        actor=actor,
        qty=entry.prints,
        article_id=entry.article_id,
        details={"entry_id": entry.id},
    )


def set_prints(session: Session, entry_id: int, prints: int, actor: Actor) -> None:
    """Change the number of prints; reducing a rule entry pauses the article (Spec 7.2)."""
    entry = _entry(session, entry_id)
    prints = _prints(prints)
    before = entry.prints
    if prints == before:
        return
    entry.prints = prints
    session.flush()
    write_journal(
        session,
        kind="queue_entry_changed",
        actor=actor,
        qty=prints,
        article_id=entry.article_id,
        details={"entry_id": entry.id, "before": before, "after": prints},
    )
    if entry.origin == "rule" and prints < before:
        pause_article(session, entry.article_id, "queue_reduced", actor)


def delete_entry(session: Session, entry_id: int, actor: Actor) -> None:
    """Delete an entry; deleting a rule entry pauses the article (Spec 7.2)."""
    entry = _entry(session, entry_id)
    article_id, origin, prints = entry.article_id, entry.origin, entry.prints
    session.delete(entry)
    session.flush()
    _renumber(session, _ordered(session))
    write_journal(
        session,
        kind="queue_entry_deleted",
        actor=actor,
        qty=prints,
        article_id=article_id,
        details={"entry_id": entry_id, "origin": origin},
    )
    if origin == "rule":
        pause_article(session, article_id, "queue_reduced", actor)
