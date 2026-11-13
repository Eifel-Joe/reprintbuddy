"""The events of the activity log that change no data (Spec 10.7 "nebenher").

`record()` and `transition()` only append to a bounded queue in memory and return at once: signing
in, a request, the automation never wait for the log. `drain()` writes what is queued in one write
transaction; the background task `run()` calls it in a worker thread. Before it goes into the thread,
the decision falls in the event loop and the run is counted as a write in flight (`begin_write`), with
no `await` in between: a restore that begins afterwards waits for it like for a request, one that
began before is seen and nothing is written (the queue keeps it for later).

Failed sign-ins from the same address within 10 minutes become one line with a count (Spec 10.7,
group 1). A transition writes a line only when the state stored in `activity_marks` changes, so a
restart logs no new change; from "unknown" to the good state is no change either.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy.activity.kinds import KINDS, Actor, area_of
from reprintbuddy.models import (
    naive_utc,
    ActivityEntry,
    ActivityMark,
    Article,
    Dispatch,
    FilamentSort,
    NotifyMark,
    Part,
    Setting,
    utcnow,
)
from reprintbuddy.ops.runner import in_thread

log = logging.getLogger(__name__)

KEEP = timedelta(days=90)  # Spec 10.7 "Aufbewahrung"
FAILED_SIGN_IN_WINDOW = timedelta(minutes=10)
MAX_PENDING = 1000  # a log that cannot write must not fill the memory
DRAIN_EVERY = 1.0  # seconds
UPDATE_EVERY = 60.0  # seconds between looks at the state file of an update
PRUNE_EVERY = 24 * 3600.0

RESTORED_MARKER = "activity-restored.json"  # left by a restore, read by the next start

# The states a transition starts from silently: "unknown" -> one of these is no change.
GOOD_STATES = frozenset({"ok"})


@dataclass(frozen=True)
class _Event:
    kind: str
    actor: Actor
    details: dict[str, Any] | None = None
    address: str | None = None
    via: str | None = None
    key: str | None = None  # a transition: only logged when the state of `key` changes
    state: str | None = None
    at: datetime = field(default_factory=utcnow)


def write_transition(
    session: Session,
    key: str,
    state: str,
    entry: ActivityEntry,
    *,
    now: datetime | None = None,
    repeat: bool = False,
) -> bool:
    """Add `entry` when the stored state of `key` changes (or always, with `repeat`); store the state.

    In the caller's transaction. True when the line was added.
    """
    at = naive_utc(now or utcnow())
    mark = session.get(ActivityMark, key)
    before = mark.state if mark is not None else None
    if mark is None:
        session.add(ActivityMark(key=key, state=state, at=at))
    else:
        mark.state, mark.at = state, at
    if before == state and not repeat:
        return False
    if before is None and state in GOOD_STATES:
        return False
    entry.at = at
    session.add(entry)
    return True


def line(kind: str, actor: Actor, details: dict[str, Any] | None = None, **extra: Any) -> ActivityEntry:
    """A line of `kind` (it must be in the fixed list)."""
    if kind not in KINDS:
        raise KeyError(kind)
    return ActivityEntry(area=area_of(kind), kind=kind, actor=actor, details=details, **extra)


class ActivityLog:
    def __init__(
        self,
        maker: sessionmaker,
        *,
        may_write: Callable[[], bool] = lambda: True,
        begin_write: Callable[[], None] = lambda: None,
        end_write: Callable[[], None] = lambda: None,
        update_result: Callable[[], dict[str, Any] | None] = lambda: None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sm = maker
        self.may_write = may_write
        self.begin_write = begin_write
        self.end_write = end_write
        self.update_result = update_result
        self.now = now
        self._pending: deque[_Event] = deque(maxlen=MAX_PENDING)
        self._lock = threading.Lock()  # record() is called from the loop and from worker threads

    # ---- in ------------------------------------------------------------------------------------

    def record(
        self,
        kind: str,
        actor: Actor,
        details: dict[str, Any] | None = None,
        *,
        address: str | None = None,
        via: str | None = None,
    ) -> None:
        """Queue one line; returns at once and never raises."""
        try:
            if kind not in KINDS:
                raise KeyError(kind)
            with self._lock:
                self._pending.append(_Event(kind, actor, details, address, via, at=naive_utc(self.now())))
        except Exception:
            log.exception("activity log: an event could not be queued")

    def transition(self, key: str, state: str, kind: str, actor: Actor, details: dict[str, Any] | None = None) -> None:
        """Queue a line that is written only when the state of `key` changes."""
        try:
            if kind not in KINDS:
                raise KeyError(kind)
            with self._lock:
                self._pending.append(_Event(kind, actor, details, key=key, state=state, at=naive_utc(self.now())))
        except Exception:
            log.exception("activity log: an event could not be queued")

    @property
    def pending(self) -> int:
        return len(self._pending)

    # ---- out -----------------------------------------------------------------------------------

    def _take(self) -> list[_Event]:
        with self._lock:
            events = list(self._pending)
            self._pending.clear()
        return events

    def _failed_sign_in(self, session: Session, event: _Event) -> None:
        """One line per address and 10 minutes from the first attempt; it stands at the last attempt,
        so a running attack is at the top and the line lives 90 days from its last attempt."""
        same_address = (
            ActivityEntry.address.is_(None) if event.address is None else ActivityEntry.address == event.address
        )
        last = session.scalars(
            select(ActivityEntry)
            .where(ActivityEntry.kind == "login_failed", same_address)
            .order_by(ActivityEntry.id.desc())
            .limit(1)
        ).first()
        if last is not None:
            details = dict(last.details or {})
            try:
                first = datetime.fromisoformat(str(details.get("first")))
            except ValueError:
                first = None
            if first is not None and event.at - first < FAILED_SIGN_IN_WINDOW:
                details["count"] = int(details.get("count", 1)) + 1
                details["last"] = event.at.isoformat()
                last.details = details
                last.at = event.at
                return
        session.add(
            ActivityEntry(
                at=event.at, area="login", kind="login_failed", actor=event.actor, address=event.address,
                via=event.via,
                details={"count": 1, "first": event.at.isoformat(), "last": event.at.isoformat()},
            )
        )

    def _write_one(self, session: Session, event: _Event) -> None:
        if event.kind == "login_failed":
            self._failed_sign_in(session, event)
            return
        entry = ActivityEntry(
            at=event.at, area=area_of(event.kind), kind=event.kind, actor=event.actor,
            details=event.details, address=event.address, via=event.via,
        )
        if event.key is None:
            session.add(entry)
            return
        mark = session.get(ActivityMark, event.key)
        if event.kind == "update_result" and (mark is None or mark.state != event.state):
            # The start of the update, from its state file too, before its result: the old process
            # writes nothing while the update runs, and a rollback would take a line of that time with it
            # (Spec 11.3a; review v0.3.13 C, finding 3).
            details = event.details or {}
            session.add(ActivityEntry(
                at=naive_utc(datetime.fromisoformat(str(event.state))), area=area_of("update_started"),
                kind="update_started", actor="user", details={"from": details.get("from"), "to": details.get("to")},
            ))
            session.flush()
        write_transition(session, event.key, str(event.state), entry, now=event.at)

    def _write(self, session: Session, events: list[_Event]) -> None:
        for event in events:
            try:
                with session.begin_nested():  # one bad event never takes the others with it
                    self._write_one(session, event)
            except Exception as error:  # noqa: BLE001
                log.warning("activity log: an event could not be written (%s)", type(error).__name__)

    def drain(self) -> int:
        """Write what is queued (blocking; a worker thread or a test calls it). Returns how many
        events were taken. A failing write drops them with a warning: the log never holds anything up."""
        events = self._take()
        if not events:
            return 0
        try:
            with self.sm.begin() as session:
                self._write(session, events)
        except Exception as error:  # noqa: BLE001 - the log must not stop anything
            log.warning("activity log: %d events could not be written (%s)", len(events), type(error).__name__)
        return len(events)

    def prune(self) -> None:
        """Delete lines older than 90 days (Spec 10.7)."""
        cutoff = naive_utc(self.now()) - KEEP
        try:
            with self.sm.begin() as session:
                session.execute(delete(ActivityEntry).where(ActivityEntry.at < cutoff))
        except Exception as error:  # noqa: BLE001
            log.warning("activity log: old lines could not be deleted (%s)", type(error).__name__)

    def check_update(self) -> None:
        """Queue the result of the last update once per update (from its state file, Spec 10.7)."""
        try:
            result = self.update_result()
        except Exception:  # noqa: BLE001 - a missing or broken file says nothing
            log.warning("activity log: the state of the last update could not be read", exc_info=True)
            return
        if result is not None:
            started = str(result.pop("started_at"))
            result.pop("finished_at", None)
            self.transition("update.result", started, "update_result", "system", result)

    # ---- the background task ---------------------------------------------------------------------

    async def drain_counted(self) -> bool:
        """One run as a write in flight: decided and counted in the loop, written in a thread.

        From `may_write()` to `begin_write()` there is no `await`: a restore that starts afterwards sees
        the count and waits; one that started before makes `may_write()` false and nothing is written.
        """
        if not self._pending:
            return False
        return await self._counted(self.drain)

    async def _counted(self, func: Callable[[], object]) -> bool:
        if not self.may_write():
            return False
        self.begin_write()  # no `await` since `may_write()`
        # The count drops when the thread is done, not when this task is cancelled (a shutdown): a
        # restore must not replace the database under a write that is still running.
        done = asyncio.ensure_future(in_thread(func))
        done.add_done_callback(lambda _future: self.end_write())
        await asyncio.shield(done)
        return True

    async def run(self) -> None:
        """Until cancelled: write every second, look at the update every minute, prune every day."""
        loop = asyncio.get_running_loop()
        next_update = next_prune = loop.time()
        try:
            while True:
                if loop.time() >= next_update:
                    self.check_update()
                    next_update = loop.time() + UPDATE_EVERY
                if loop.time() >= next_prune:
                    await self._counted(self.prune)
                    next_prune = loop.time() + PRUNE_EVERY
                await self.drain_counted()
                await asyncio.sleep(DRAIN_EVERY)
        finally:
            # A last run at shutdown, counted like every other (the shutdown waits a few seconds).
            with contextlib.suppress(BaseException):
                await self.drain_counted()


def read_restored_marker(data_dir: Path) -> dict[str, Any] | None:
    """What a restore left for the next start (and remove it): the date and version of the backup, and
    the marks of the database it replaced: those of the log (`marks`) and those of the push messages
    (`notify_marks`), each None when there were none to read."""
    path = data_dir / RESTORED_MARKER
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        path.unlink()
    except OSError:
        log.warning("activity log: the restore marker could not be removed")
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    marks = data.get("marks")
    notify_marks = data.get("notify_marks")
    return {
        **{key: data.get(key) for key in ("created_at", "version", "via")},
        "marks": marks if isinstance(marks, dict) else None,
        "notify_marks": notify_marks if isinstance(notify_marks, list) else None,
    }


def write_restored_marker(
    data_dir: Path,
    *,
    created_at: datetime,
    version: str,
    via: str | None,
    marks: dict[str, dict[str, str | None]] | None = None,
    notify_marks: list[dict[str, Any]] | None = None,
) -> None:
    """Left after a restore replaced the database; the next start logs it into the restored one."""
    content: dict[str, Any] = {"created_at": created_at.isoformat(), "version": version, "via": via}
    if marks is not None:
        content["marks"] = marks
    if notify_marks is not None:
        content["notify_marks"] = notify_marks
    try:
        (data_dir / RESTORED_MARKER).write_text(json.dumps(content), encoding="utf-8")
    except OSError:
        log.warning("activity log: the restore marker could not be written")


# p186 (v0.3.14). Root: which states the log has written (`activity_marks`) is kept in the database, so a
# restore brought back the marks of the backup; the next start wrote a state again that the log had shown
# long before (Bambuddy unreachable, a version found, the result of the last update). The marks belong to
# what has happened, not to the data: the restore reads them from the database it replaces (`current_marks`)
# and leaves them in the restore marker; the next start sets them into the restored database, after its
# migration and before anything is logged (`restore_marks`).
# Rejected: writing the marks into the restored file at once - an older backup may not have the table yet,
# it gets it only from the migration at the next start.
# Tests: tests/activity/test_activity_ops.py (p186), tests/ops/test_restorecmd.py (marks_of_the_log).


def _iso(value: object) -> str | None:
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _rows(db_path: Path, query: str) -> list[tuple[Any, ...]] | None:
    """The rows of `query` in the database at `db_path`, read only; None when they cannot be read."""
    try:
        connection = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        return connection.execute(query).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def current_marks(db_path: Path) -> dict[str, dict[str, str | None]] | None:
    """The marks of the log in the database at `db_path`, read only; None when they cannot be read (no
    database, or one of a version before the log)."""
    rows = _rows(db_path, "SELECT key, state, at FROM activity_marks")
    if rows is None:
        return None
    return {str(key): {"state": str(state), "at": _iso(at)} for key, state, at in rows}


# p220 (v0.3.15). Root: which events were reported to Home Assistant is kept in the database too
# (`notify_marks`, Spec 10.6); restored from an older backup, an event reported since (a print done, an
# article below its minimum) had no mark or one still `due`, and went to the phone once more. Same way as
# p186: the restore reads the marks of the database it replaces and leaves them in the restore marker; the
# next start makes them the marks of the restored database, after its migration and before the first tick.
# The whole table is taken over, `due` and `watch` included: they describe what happened, not the data.
# Two limits (review v0.3.15 D, findings 1 and 2):
# - Ids are not kept for ever: SQLite hands out max(id)+1, so after an older backup the next print, article
#   or part gets an id the replaced database had used. A mark of a thing the restored database does not have
#   would mute that new one (for 30 days for a print); such marks are left out (`_still_there`).
# - Switched off, the messages keep no marks (`events.forget_all`) and switching on relies on that. The marks
#   are carried only when the messages are on in both databases; otherwise the restored one keeps its own.
# Tests: tests/activity/test_activity_ops.py (p220), tests/ops/test_restorecmd.py (push_messages),
# tests/notify/test_notify_restore_marks.py.

_NOTIFY_FIELDS = ("kind", "ref", "state", "streak", "first_seen_at", "notified_at", "gone_since")
_NOTIFY_MOMENTS = ("first_seen_at", "notified_at", "gone_since")


def current_notify_marks(db_path: Path) -> list[dict[str, Any]] | None:
    """The marks of the push messages in the database at `db_path`, read only; None when they cannot be
    read (no database, or one of a version before the push messages) or when the messages are switched
    off there (then it has none, and the restored database keeps its own)."""
    enabled = _rows(db_path, "SELECT value FROM settings WHERE key = 'ha_enabled'")
    if not enabled or enabled[0][0] != "true":
        return None
    rows = _rows(db_path, f"SELECT {', '.join(_NOTIFY_FIELDS)} FROM notify_marks ORDER BY id")
    if rows is None:
        return None
    return [
        {
            name: (_iso(value) if value is not None else None) if name in _NOTIFY_MOMENTS else value
            for name, value in zip(_NOTIFY_FIELDS, row, strict=True)
        }
        for row in rows
    ]


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return naive_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


_PRINT_KINDS = ("print_done", "print_failed")  # notify.events.ONCE_PER_PRINT (that module imports the web)


def _still_there(session: Session, kind: str, ref: str) -> bool:
    """Does the restored database have the thing a mark is about? Warnings and rights are no things."""
    model: type | None = None
    if kind in _PRINT_KINDS:
        model, key = Dispatch, ref
    elif kind in ("paused", "below_min"):
        model, key = Article, ref
    elif kind == "reorder":
        item_type, _, key = ref.partition(":")
        model = {"part": Part, "filament": FilamentSort}.get(item_type)
        if model is None:
            return False
    if model is None:
        return True
    return key.isdigit() and session.get(model, int(key)) is not None


def restore_notify_marks(maker: sessionmaker, marks: list[Any]) -> None:
    """Make the marks of the push messages of the restored database those of the one it replaced (in one
    write transaction), when the messages are on there; marks of things it does not have are left out. A
    row that does not fit is left out too; it would at worst report once more."""
    try:
        with maker.begin() as session:
            enabled = session.get(Setting, "ha_enabled")
            if enabled is None or json.loads(enabled.value) is not True:
                return  # switched off: no marks, as `forget_all` left them
            session.execute(delete(NotifyMark))
            seen: set[tuple[str, str]] = set()
            for mark in marks:
                if not isinstance(mark, dict):
                    continue
                kind, ref, state = mark.get("kind"), mark.get("ref"), mark.get("state")
                if not (isinstance(kind, str) and isinstance(ref, str) and state in ("watch", "due", "sent")):
                    continue
                if (kind, ref) in seen or not _still_there(session, kind, ref):
                    continue
                seen.add((kind, ref))
                streak = mark.get("streak")
                session.add(
                    NotifyMark(
                        kind=kind,
                        ref=ref,
                        state=state,
                        streak=streak if isinstance(streak, int) and streak >= 0 else 0,
                        first_seen_at=_moment(mark.get("first_seen_at")) or utcnow(),
                        notified_at=_moment(mark.get("notified_at")),
                        gone_since=_moment(mark.get("gone_since")),
                    )
                )
    except Exception as error:  # noqa: BLE001 - the push messages must not stop the start
        log.warning("push messages: the marks could not be carried over (%s)", type(error).__name__)


def restore_marks(maker: sessionmaker, marks: dict[str, Any]) -> None:
    """Make the marks of the restored database those of the one it replaced (in one write transaction)."""
    try:
        with maker.begin() as session:
            session.execute(delete(ActivityMark))
            for key, mark in marks.items():
                if not isinstance(key, str) or not isinstance(mark, dict) or not isinstance(mark.get("state"), str):
                    continue
                at = mark.get("at")
                try:
                    moment = datetime.fromisoformat(at) if isinstance(at, str) else utcnow()
                except ValueError:
                    moment = utcnow()
                session.add(ActivityMark(key=key, state=mark["state"], at=naive_utc(moment)))
    except Exception as error:  # noqa: BLE001 - the log must not stop the start
        log.warning("activity log: the marks could not be carried over (%s)", type(error).__name__)
