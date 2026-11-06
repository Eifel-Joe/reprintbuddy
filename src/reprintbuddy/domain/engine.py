"""The engine: rule 7.1, hand-over and polling in one locked tick (Spec 7.1, 7.3, 7.6, 7.7).

Plan B4 decisions 4, 5, 12, 14-17. In the app the one instance is `app.state.ticker`.

`tick(poll=True)` (regular: at start and every `poll_minutes`) settles unclear
hand-overs, asks Bambuddy about every open print, checks print files and targets, reads
Bambuddy's queue for display, runs the hooks (B6), applies rule 7.1 and hands prints over.
`tick(poll=False)` (after a booking) only settles, applies the rule and hands over.

Safety:
- One `asyncio.Lock` around everything after the update check: two ticks never
  interleave, so a print in the middle of its hand-over is never taken for "never sent"
  by `resolve_pending` in a second tick (review focus 2).
- Nothing new is handed over while a hand-over is unclear, Bambuddy is unreachable,
  a poll was refused, or - until the next regular tick - Bambuddy rejected a print.
- During an update the tick does nothing at all: no Bambuddy call, no write.

The runtime state lives only in memory (`EngineStatus`, decision 14); the first tick
after a restart fills it again.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy import settings
from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import QueueItem
from reprintbuddy.domain import dispatch, tracking, workqueue
from reprintbuddy.models import Article, Dispatch, WorkQueueEntry, utcnow
from reprintbuddy.i18n import translate

log = logging.getLogger(__name__)

PollHook = Callable[[BambuddyClient, sessionmaker, datetime], Awaitable[None]]
Publish = Callable[[frozenset[str]], None]

TOPIC = "bambuddy"
# Poll interval until `poll_seconds()` could be read once (it reads the settings).
DEFAULT_POLL_SECONDS = 60.0


@dataclass
class EngineStatus:
    """What the last ticks found; shown as warnings (Spec 10.2) and on the queue page."""

    configured: bool = False
    reachable: bool | None = None
    last_poll_at: datetime | None = None
    last_tick_at: datetime | None = None
    unexpected: list[str] = field(default_factory=list)
    waiting_review: list[int] = field(default_factory=list)
    rejected: str | None = None
    poll_error: str | None = None
    unresolved: int = 0
    duplicates: list[list[int]] = field(default_factory=list)
    cap_reached: bool = False
    skipped_for_update: bool = False
    bambuddy_queue: list[QueueItem] | None = None
    bambuddy_queue_at: datetime | None = None
    hook_errors: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    # Articles whose printer is in maintenance mode (assumption A9): skipped, not paused.
    # Set by regular ticks that ran the article check; other ticks keep the last answer.
    waiting_for_printer: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class TickResult:
    skipped: Literal["update"] | None
    created: int  # work queue entries made by rule 7.1
    dispatched: int  # prints handed over to Bambuddy


def _snapshot(status: EngineStatus) -> tuple:
    """The displayed fields, copied: everything except `last_tick_at`."""
    return tuple(
        copy.deepcopy(getattr(status, f.name))
        for f in dataclasses.fields(status)
        if f.name != "last_tick_at"
    )


class Engine:
    def __init__(
        self,
        sm: sessionmaker,
        client_provider: Callable[[], BambuddyClient | None],
        *,
        update_running: Callable[[], bool] = lambda: False,
        now: Callable[[], datetime] = utcnow,
        publish: Publish | None = None,
    ) -> None:
        self.sm = sm
        self.client_provider = client_provider
        self.update_running = update_running
        self.now = now
        self.publish = publish
        self.status = EngineStatus()
        # Known before the first tick, so a page right after the start does not say
        # "not configured" wrongly. A provider that fails leaves it False; the tick tells.
        try:
            self.status.configured = client_provider() is not None
        except Exception:
            log.warning("engine: the Bambuddy client could not be made", exc_info=True)
        self.hooks: dict[str, PollHook] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._poll_requested = False
        self._published = _snapshot(self.status)

    # ---- outside interface ---------------------------------------------------------------

    def add_poll_hook(self, name: str, hook: PollHook) -> None:
        """Run `hook` in every regular tick with Bambuddy reachable (decision 17, for B6)."""
        self.hooks[name] = hook

    def request_tick(self) -> None:
        """Ask `run` for a tick soon (after a booking); it does not move the next poll."""
        self._wake.set()

    def request_poll(self) -> None:
        """Ask `run` for a regular tick now (e.g. Bambuddy reported a change)."""
        self._poll_requested = True
        self._wake.set()

    # ---- the tick ------------------------------------------------------------------------

    async def tick(self, *, poll: bool) -> TickResult:
        try:
            return await self._tick(poll)
        finally:
            self._announce()

    async def _tick(self, poll: bool) -> TickResult:
        if self.update_running():
            self.status.skipped_for_update = True
            return TickResult("update", 0, 0)
        waited = self._lock.locked()
        async with self._lock:
            # An update may have started while this tick waited for the other one.
            if waited and self.update_running():
                self.status.skipped_for_update = True
                return TickResult("update", 0, 0)
            self.status.skipped_for_update = False
            return await self._locked_tick(poll)

    async def _locked_tick(self, poll: bool) -> TickResult:
        status = self.status
        client = self.client_provider()
        status.configured = client is not None
        if client is None:
            status.reachable = None
        else:
            await self._settle(client)
            if poll and status.reachable:
                await self._poll(client)

        with self.sm.begin() as session:
            created = len(workqueue.apply_rule(session))

        dispatched = 0
        if client is not None and self._may_hand_over(poll):
            dispatched = await self._hand_over(client)

        with self.sm() as session:
            status.cap_reached = self._cap_reached(session)
        status.last_tick_at = self.now()
        return TickResult(None, created, dispatched)

    async def _settle(self, client: BambuddyClient) -> None:
        """Step 4: reachability, then every `sending`/`unclear` print (decision 5)."""
        status = self.status
        reachable = True
        if status.reachable is not True:
            try:
                reachable = bool(await client.ping())
            except BambuddyUnavailable:
                reachable = False
        report = await dispatch.resolve_pending(client, self.sm)
        status.reachable = reachable and report.reachable
        status.unresolved = report.unresolved
        # Duplicates are reported once, when they are settled: keep them until the next
        # report names new ones (a restart clears them; the article stays paused).
        if report.duplicates:
            status.duplicates = report.duplicates

    async def _poll(self, client: BambuddyClient) -> None:
        """Step 5: ask Bambuddy about everything; a refusal ends only its own part.

        Decision 15: only a poll that reaches its end clears `poll_error`. A poll cut short
        (Bambuddy unreachable) keeps the previous refusal - or records a new one found
        before the cut - so a later short tick with a good ping hands nothing over until
        a complete poll has run.
        """
        status = self.status
        error: str | None = None

        def cut_short() -> None:
            status.reachable = False
            if error is not None:
                status.poll_error = error

        report = await tracking.poll_dispatches(client, self.sm)
        if report.error is not None:
            error = report.error
        if not report.reachable:
            # Also keeps the last complete `unexpected`/`waiting_review` for display.
            cut_short()
            return
        status.unexpected = report.unexpected
        status.waiting_review = report.waiting_review

        check = await tracking.check_articles(client, self.sm)
        if check.error == "unavailable":
            cut_short()
            return
        if check.error is not None:
            error = check.error
        else:
            status.waiting_for_printer = sorted(check.waiting_for_printer)

        try:
            status.bambuddy_queue = await client.list_queue()
            status.bambuddy_queue_at = self.now()
        except BambuddyUnavailable:
            cut_short()
            return
        except BambuddyRejected as exc:
            error = str(exc)
        status.poll_error = error  # the poll reached its end

        for name, hook in list(self.hooks.items()):
            try:
                await hook(client, self.sm, self.now())
            except Exception as exc:  # a hook never stops the tick (decision 17)
                log.exception("engine: poll hook %s failed", name)
                status.hook_errors[name] = repr(exc)
            else:
                status.hook_errors.pop(name, None)

        status.rejected = None  # decision 4: a regular tick tries again
        if status.poll_error is None:
            status.last_poll_at = self.now()

    def _may_hand_over(self, poll: bool) -> bool:
        status = self.status
        return (
            status.reachable is True
            and status.unresolved == 0
            and status.poll_error is None
            and not (status.rejected is not None and not poll)
        )

    async def _hand_over(self, client: BambuddyClient) -> int:
        """Step 7: one print at a time until the cap, nothing eligible, or a failure."""
        status = self.status
        skip = frozenset(status.waiting_for_printer)
        dispatched = 0
        while True:
            # Settings are read again for every print: a stop or a lower cap set in the
            # middle of a tick holds for the next print already.
            with self.sm.begin() as session:
                if not settings.get_setting(session, "queue_running"):
                    return dispatched
                cap = int(settings.get_setting(session, "queue_cap"))
                insert_next = bool(settings.get_setting(session, "insert_next"))
                res = dispatch.reserve_next(session, cap=cap, skip_article_ids=skip)
            if res is None:
                return dispatched
            result = await dispatch.hand_over(client, self.sm, res, insert_next=insert_next)
            if result == "queued":
                dispatched += 1
                continue
            if result == "rejected":
                with self.sm() as session:
                    status.rejected = session.get(Dispatch, res.dispatch_id).message
            else:  # "unclear" or "not_sent": Bambuddy did not answer
                status.reachable = False
            return dispatched

    def _cap_reached(self, session: Session) -> bool:
        """Step 8: the cap holds back a print that could otherwise go now."""
        if not settings.get_setting(session, "queue_running"):
            return False
        cap = int(settings.get_setting(session, "queue_cap"))
        if dispatch.open_count(session) < cap:
            return False
        skip = set(self.status.waiting_for_printer)
        waiting = session.execute(
            select(Article.id, Article.target_model, Article.printer_id)
            .join(WorkQueueEntry, WorkQueueEntry.article_id == Article.id)
            .where(
                WorkQueueEntry.held.is_(False),
                Article.paused.is_(False),
                Article.archived_at.is_(None),
            )
        ).all()
        return any(
            row.id not in skip and (row.target_model or row.printer_id is not None)
            for row in waiting
        )

    def _announce(self) -> None:
        """Tell open pages when what they show of the status changed (Spec 7.6, 10.5)."""
        current = _snapshot(self.status)
        if current == self._published:
            return
        self._published = current
        if self.publish is None:
            return
        try:
            self.publish(frozenset({TOPIC}))
        except Exception:  # the tick stands; a page that misses it catches up on reconnect
            log.warning("engine: publishing the status failed", exc_info=True)

    # ---- the loop ------------------------------------------------------------------------

    async def run(self, poll_seconds: Callable[[], float]) -> None:
        """Tick until cancelled: a regular tick at once and every `poll_seconds()`, a short
        one on `request_tick()`. Requests never push the next poll back."""
        loop = asyncio.get_running_loop()
        next_poll_at = loop.time()
        interval = DEFAULT_POLL_SECONDS
        while True:
            wait = next_poll_at - loop.time()
            if wait > 0 and not self._poll_requested:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                except TimeoutError:
                    pass
            self._wake.clear()
            poll = self._poll_requested or loop.time() >= next_poll_at
            problem: str | None = None
            if poll:
                self._poll_requested = False
                # Read from the settings: a locked database must not end the loop. The
                # last interval (at first the default) holds until it can be read again.
                try:
                    interval = float(poll_seconds())
                except Exception as exc:
                    log.exception("engine: the poll interval could not be read")
                    problem = repr(exc)
                next_poll_at = loop.time() + interval
            try:
                await self.tick(poll=poll)
            except Exception as exc:
                log.exception("engine: tick failed")
                problem = repr(exc)
            self.status.error = problem
            self._announce()


# ---- warnings --------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineWarning:
    key: str
    params: dict[str, object]


def collect_warnings(session: Session, status: EngineStatus) -> list[EngineWarning]:
    """Spec 10.2 "warnings", in a fixed order; B7 shows the same list.

    `article_paused` carries the pause reason already translated into the interface
    language; lists stay lists (see `warning_text`).
    """
    found: list[EngineWarning] = []

    def add(key: str, **params: object) -> None:
        found.append(EngineWarning(f"warning.{key}", params))

    if not status.configured:
        add("bambuddy_not_configured")
    elif status.reachable is False:
        add("bambuddy_unreachable")
    if status.poll_error:
        add("poll_rejected", message=status.poll_error)
    if status.unexpected:
        add("unexpected_answer", statuses=list(status.unexpected))
    if status.waiting_review:
        add("waiting_for_review", count=len(status.waiting_review))
    if status.rejected:
        add("bambuddy_rejected", message=status.rejected)
    unclear = int(
        session.scalar(
            select(func.count()).select_from(Dispatch).where(Dispatch.status == "unclear")
        )
    )
    if unclear:
        add("unclear_prints", count=unclear)
    if status.duplicates:
        add("duplicates", ids=[list(ids) for ids in status.duplicates])
    if status.error:
        add("tick_error", message=status.error)
    if status.hook_errors:
        add("hook_error", names=sorted(status.hook_errors))
    if status.skipped_for_update:
        add("update_running")
    if not settings.get_setting(session, "queue_running"):
        add("queue_stopped")
    if status.cap_reached:
        add("cap_reached", cap=int(settings.get_setting(session, "queue_cap")))
    if status.waiting_for_printer:
        names = session.scalars(
            select(Article.name)
            .where(Article.id.in_(status.waiting_for_printer))
            .order_by(Article.name, Article.id)
        ).all()
        if names:
            add("waiting_for_printer", names=list(names))

    lang = str(settings.get_setting(session, "language"))
    paused = session.execute(
        select(Article.name, Article.pause_reason)
        .where(Article.paused.is_(True), Article.archived_at.is_(None))
        .order_by(Article.name, Article.id)
    ).all()
    for name, reason in paused:
        text = translate(lang, f"pause.reason.{reason}") if reason else ""
        add("article_paused", name=name, reason=text)
    return found


def _param_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        if any(isinstance(v, (list, tuple)) for v in value):
            return "; ".join(_param_text(v) for v in value)
        return ", ".join(str(v) for v in value)
    return str(value)


def warning_text(lang: str, warning: EngineWarning) -> str:
    """The warning as text in `lang`; lists are joined with ", " (groups with "; ")."""
    params = {name: _param_text(value) for name, value in warning.params.items()}
    return translate(lang, warning.key, **params)
