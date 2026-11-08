"""The change feed: every committed change, reported as topics (kind and id, no content).

The listeners hang on the session events of the application's one sessionmaker, so no
place that writes through a session from it can forget to report. `after_flush` collects
the rows of every flush, `do_orm_execute` the kind of a bulk statement (it cannot name the
rows, which is why the domain writes row by row), `after_commit` publishes once per
transaction and `after_rollback` discards. Raw SQL is not seen; tests forbid it outside
the migrations.

Topics: `article`, `article:<id>`, `article-settings:<id>`, `part`, `part:<id>`,
`part-settings:<id>`, `filament-settings:<sort id>`, `set`, `set:<id>`, `order`, `order:<id>`, `journal`, `settings`,
`queue`, `inspection`, `filament`, `reorder`, `bambuddy`.
`…-settings:<id>` only when a column of the edit form changes, so a booking does not tell
an open edit form that its article changed when only the stock did.

A booking never fails because of the feed: errors are logged, not raised. Where a case is
unclear, the feed reports: one notice too many costs a region fetch, a missing one leaves
a page wrong.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from sqlalchemy import event, inspect
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker

from reprintbuddy.models import (
    Article,
    ArticleFilament,
    ArticleFilamentSort,
    ArticlePart,
    Dispatch,
    FilamentSettings,
    FilamentSnapshot,
    FilamentSort,
    FormToken,
    Inspection,
    JournalEntry,
    LoginSession,
    Order,
    OrderLine,
    Part,
    ProductSet,
    ReorderMark,
    SetPosition,
    SetPositionOption,
    Setting,
    User,
    WorkQueueEntry,
)

log = logging.getLogger(__name__)

Publish = Callable[[frozenset[str]], None]
Change = Literal["new", "dirty", "deleted"]

IGNORED: frozenset[type] = frozenset({User, LoginSession, FormToken})

# The kind of each table; a bulk statement is reported with its kind only.
_KIND: dict[type, str] = {
    Article: "article",
    ArticleFilament: "article",
    ArticleFilamentSort: "article",
    ArticlePart: "article",
    Part: "part",
    ProductSet: "set",
    SetPosition: "set",
    SetPositionOption: "set",
    Order: "order",
    OrderLine: "order",
    JournalEntry: "journal",
    Setting: "settings",
    WorkQueueEntry: "queue",
    Dispatch: "queue",
    Inspection: "inspection",
    FilamentSort: "filament",
    FilamentSettings: "filament",
    FilamentSnapshot: "filament",
    ReorderMark: "reorder",
}

CLASSIFIED: frozenset[type] = frozenset(_KIND) | IGNORED

# The columns the edit forms show (Design 4.6).
SETTINGS_COLUMNS: dict[type, frozenset[str]] = {
    Article: frozenset(
        {
            "name",
            "sku",
            "label_text",
            "units_per_print",
            "min_stock",
            "buffer",
            "target_model",
            "printer_id",
            "needs_assembly",
            "auto_enabled",
            "archived_at",
        }
    ),
    Part: frozenset(
        {
            "name",
            "unit",
            "min_stock",
            "pack_size",
            "min_order_packs",
            "lead_time_days",
            "safety_value",
            "safety_unit",
            "supplier",
            "order_url",
            "archived_at",
        }
    ),
}

_TOPICS_KEY = "live_topics"


def _settings_changed(obj: object, change: Change) -> bool:
    if change != "dirty":
        return True
    attrs = inspect(obj).attrs
    return any(attrs[column].history.has_changes() for column in SETTINGS_COLUMNS[type(obj)])


def topics_of(obj: object, change: Change) -> set[str]:
    """The topics one changed row reports; ignored tables report nothing."""
    cls = type(obj)
    if cls in IGNORED:
        return set()
    if cls not in _KIND:
        raise TypeError(f"{cls.__name__} has no topics in the change feed")
    if cls in (Article, Part):
        kind = _KIND[cls]
        topics = {kind, f"{kind}:{obj.id}"}
        if _settings_changed(obj, change):
            topics.add(f"{kind}-settings:{obj.id}")
        return topics
    if cls in (ArticleFilament, ArticlePart):
        return {"article", f"article:{obj.article_id}", f"article-settings:{obj.article_id}"}
    if cls is ArticleFilamentSort:
        # The filament page lists the ticked spool sorts (Spec 9.3).
        return {"article", f"article:{obj.article_id}", f"article-settings:{obj.article_id}", "filament"}
    if cls is FilamentSettings:
        # The settings form of a sort watches only its own settings; the heartbeat writes
        # day rows of other tables, which must not tell an open form that it changed.
        return {"filament", f"filament-settings:{obj.spool_filament_id}"}
    if cls is ProductSet:
        return {"set", f"set:{obj.id}"}
    if cls is SetPosition:
        return {"set", f"set:{obj.set_id}"}
    if cls is Order:
        return {"order", f"order:{obj.id}"}
    if cls is OrderLine:
        return {"order", f"order:{obj.order_id}"}
    if cls in (WorkQueueEntry, Dispatch, Inspection):
        # Cover and status of an article hang on these rows (Spec 10.1).
        return {_KIND[cls], "article", f"article:{obj.article_id}"}
    return {_KIND[cls]}


def _collect(session: Session, topics: set[str]) -> None:
    if topics:
        session.info.setdefault(_TOPICS_KEY, set()).update(topics)


def install_change_feed(maker: sessionmaker, publish: Publish) -> None:
    """Report every committed change of sessions from `maker` through `publish`."""

    @event.listens_for(maker, "after_flush")
    def _after_flush(session: Session, _flush_context) -> None:
        changed: list[tuple[object, Change]] = [(obj, "new") for obj in session.new]
        changed += [(obj, "dirty") for obj in session.dirty if session.is_modified(obj)]
        changed += [(obj, "deleted") for obj in session.deleted]
        topics: set[str] = set()
        for obj, change in changed:
            try:
                topics |= topics_of(obj, change)
            except TypeError:
                log.exception("change feed: a row could not be reported")
        _collect(session, topics)

    @event.listens_for(maker, "do_orm_execute")
    def _do_orm_execute(state: ORMExecuteState) -> None:
        if not (state.is_insert or state.is_update or state.is_delete):
            return
        mapper = state.bind_mapper
        cls = mapper.class_ if mapper is not None else None
        if cls in IGNORED:
            return
        if cls not in _KIND:
            log.error("change feed: a bulk statement on %r could not be reported", cls)
            return
        _collect(state.session, {_KIND[cls]})

    @event.listens_for(maker, "after_commit")
    def _after_commit(session: Session) -> None:
        if session.in_nested_transaction():  # a savepoint released; nothing is saved yet
            return
        topics = session.info.pop(_TOPICS_KEY, None)
        if not topics:
            return
        try:
            publish(frozenset(topics))
        except Exception:  # the commit stands; a page that misses it catches up on reconnect
            log.warning("change feed: publishing %d topics failed", len(topics), exc_info=True)

    @event.listens_for(maker, "after_rollback")
    def _after_rollback(session: Session) -> None:
        if session.in_nested_transaction():
            # Only a savepoint was undone. What it collected stays: telling a page once
            # too often costs a fetch, while dropping the outer transaction's topics
            # would leave a page wrong.
            return
        session.info.pop(_TOPICS_KEY, None)
