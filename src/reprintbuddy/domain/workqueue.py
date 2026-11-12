"""Entries of Reprintbuddy's own work queue (Spec 7.2, 10, plan B4 decision 11).

Positions are integers, smaller = earlier; after every change they are renumbered
0 to n-1. Moving writes no journal line. Functions never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy import settings
from reprintbuddy.domain import rules
from reprintbuddy.domain import articlesets
from reprintbuddy.domain.errors import NotFoundError, ValidationError
from reprintbuddy.domain.pauses import pause_article
from reprintbuddy.domain.stock import Actor, write_journal
from reprintbuddy.models import Article, Dispatch, WorkQueueEntry

Origin = Literal["rule", "scrap", "failure", "manual"]
MAX_PRINTS = 1000
# Dispatch states that still occupy Bambuddy (Spec 7.1 "in Arbeit").
OPEN_STATES: frozenset[str] = frozenset({"sending", "unclear", "queued", "printing"})


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
    if article.kind != "print":  # an article made of parts is never printed: its parts are (Spec 7.4)
        raise ValidationError("queue.print_only", name=article.name)
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


# ---- coverage and rule 7.1 -------------------------------------------------------------


def queued_prints(session: Session) -> dict[int, int]:
    """Sum of `prints` per article, held entries included."""
    rows = session.execute(
        select(WorkQueueEntry.article_id, func.sum(WorkQueueEntry.prints)).group_by(
            WorkQueueEntry.article_id
        )
    )
    return {article_id: int(total) for article_id, total in rows}


def in_progress_prints(session: Session) -> dict[int, int]:
    """Number of open prints per article."""
    rows = session.execute(
        select(Dispatch.article_id, func.count())
        .where(Dispatch.status.in_(OPEN_STATES))
        .group_by(Dispatch.article_id)
    )
    return {article_id: int(total) for article_id, total in rows}


def in_progress_units(session: Session) -> dict[int, int]:
    """Sum of `units` of the open prints per article (as reserved at dispatch time)."""
    rows = session.execute(
        select(Dispatch.article_id, func.sum(Dispatch.units))
        .where(Dispatch.status.in_(OPEN_STATES))
        .group_by(Dispatch.article_id)
    )
    return {article_id: int(total) for article_id, total in rows}


@dataclass(frozen=True)
class Coverage:
    available: int
    queued_units: int
    in_progress_units: int
    provisional: int
    awaiting_assembly: int

    @property
    def total(self) -> int:
        return rules.coverage(
            self.available,
            self.queued_units,
            self.in_progress_units,
            self.provisional,
            self.awaiting_assembly,
        )


def _coverage(article: Article, queued: int, in_progress: int) -> Coverage:
    return Coverage(
        available=rules.available(article.stock, article.reserved),
        queued_units=queued * article.units_per_print,
        in_progress_units=in_progress,
        provisional=article.provisional,
        awaiting_assembly=article.awaiting_assembly,
    )


def coverage_of(session: Session, article: Article) -> Coverage:
    queued = session.scalar(
        select(func.coalesce(func.sum(WorkQueueEntry.prints), 0)).where(
            WorkQueueEntry.article_id == article.id
        )
    )
    units = session.scalar(
        select(func.coalesce(func.sum(Dispatch.units), 0)).where(
            Dispatch.article_id == article.id, Dispatch.status.in_(OPEN_STATES)
        )
    )
    return _coverage(article, int(queued), int(units))


def coverage_all(session: Session) -> dict[int, Coverage]:
    """Coverage of every active print article: one query per table."""
    queued = queued_prints(session)
    units = in_progress_units(session)
    articles = session.scalars(articlesets.print_articles())
    return {a.id: _coverage(a, queued.get(a.id, 0), units.get(a.id, 0)) for a in articles}


def apply_rule(session: Session, actor: Actor = "auto") -> list[WorkQueueEntry]:
    """Rule 7.1: queue reprints for articles whose coverage is below the minimum stock. A part of
    articles made of parts counts with minimum + demand from assembly (Spec 7.1, 0.4.0 p341)."""
    from reprintbuddy.domain import assembled  # assembled builds on this module

    if not settings.get_setting(session, "automation"):
        return []
    coverages = coverage_all(session)
    demand = assembled.assembly_demand(session)
    articles = session.scalars(
        articlesets.print_articles()  # articles made of parts are never printed (Spec 7.1, 0.4.0)
        .where(Article.auto_enabled.is_(True), Article.paused.is_(False))
        .order_by(Article.id)
    ).all()
    created: list[WorkQueueEntry] = []
    for article in articles:
        c = coverages[article.id]
        d = demand.get(article.id, 0)
        n = rules.prints_needed(
            min_stock=article.min_stock + d,
            buffer=article.buffer,
            coverage=c.total,
            units_per_print=article.units_per_print,
        )
        if n > 0:
            # One entry holds at most MAX_PRINTS; the capped entry counts into the
            # coverage, so the next run tops up the rest. Raising here would stop the
            # rule for every other article.
            details = {"coverage": c.total, "target": article.min_stock + d + article.buffer}
            if d:
                details["demand"] = d
            if n > MAX_PRINTS:
                n = MAX_PRINTS
                details["capped"] = True
            created.append(
                add_entry(
                    session,
                    article.id,
                    n,
                    "rule",
                    actor,
                    where="end",
                    details=details,
                )
            )
    return created
