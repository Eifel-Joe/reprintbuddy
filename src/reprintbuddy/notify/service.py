"""Sending the push messages after each tick (Spec 10.6 "Senden und Fehler", plan v0.3.12 B D3).

`after_tick` is an engine listener (`Engine.after_tick`). It runs synchronously, with the engine's
lock already free and without any `await`: it brings the marks up to date (`events.detect`) and,
when nothing is being sent, picks what is due and starts **one** task that sends it. So:

- Nothing is ever sent under the engine's lock or with a transaction open; a hanging Home
  Assistant holds up neither the tick nor a booking (Spec 10.6, Anti-Ziel "Eine Nachricht hält …").
- At most one sending task at a time (condition 1 of the plan's approval). A tick during a send
  only updates the marks and asks for one more round; that round leaves out every mark the task
  has already tried, so nothing goes out twice and a failing Home Assistant is not hammered.
- The database is touched only in synchronous blocks that begin with `update_started()`, the same
  cheap question `Engine._may_send` asks: a restore or update that has begun sets it before it
  waits for the engine's lock, and nothing can run between the question and the write. Rejected:
  doing the detection in a worker thread - the thread could still be writing while a restore
  replaces the database (condition 2 of the approval; the time is measured in the report).

Outcomes (Spec 10.6): "accepted" clears the warning "Home Assistant not reachable"; any other, an
"unclear" timeout included (condition 3), sets it, and a state stays due for the next tick, so a
message whose answer was unclear can arrive twice. The warning also goes when the check `GET /api/`
succeeds at a regular query, or when the messages are switched off.

The status lives in memory like the engine's (`EngineStatus`): after a restart the next tick finds
out again. It goes to open pages as topic `notify`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy import settings as st
from reprintbuddy.domain.engine import EngineStatus
from reprintbuddy.models import utcnow
from reprintbuddy.notify import events as ev
from reprintbuddy.notify import homeassistant as ha

log = logging.getLogger(__name__)

TOPIC = "notify"
Publish = Callable[[frozenset[str]], None]


@dataclass
class NotifyStatus:
    last_at: datetime | None = None  # the last message (not the test message), naive UTC
    last_outcome: str | None = None
    problem: str | None = None  # the outcome behind the warning; None: no warning


@dataclass(frozen=True)
class _Target:
    url: str
    token: str
    service: str


def _target(session: Session) -> _Target | None:
    if not st.ha_configured(session):
        return None
    return _Target(
        str(st.get_setting(session, "ha_url")),
        str(st.get_setting(session, "ha_token")),
        str(st.get_setting(session, "ha_service")),
    )


class Notifier:
    def __init__(
        self,
        sm: sessionmaker,
        *,
        http_factory: ha.HttpFactory,
        engine_status: Callable[[], EngineStatus],
        rights: Callable[[], ev.Rights],
        update_started: Callable[[], bool],
        base: Callable[[Session], str | None],
        publish: Publish | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sm = sm
        self.http_factory = http_factory
        self.engine_status = engine_status
        self.rights = rights
        self.update_started = update_started
        self.base = base
        self.publish = publish
        self.now = now
        self.status = NotifyStatus()
        self.task: asyncio.Task[None] | None = None
        self._detect = ev.detect  # tests look at the context through this
        self._again = False  # a tick came while sending: one more round
        self._last_ctx: ev.Context | None = None
        self._seen_poll_at: datetime | None = None
        self._stopped = False

    # ---- after the tick (synchronous) ------------------------------------------------------------

    def _context(self, session: Session, poll: bool, now: datetime) -> ev.Context:
        status = self.engine_status()
        # The spool list was read when this regular tick's query reached its end (a new
        # `last_poll_at`) and the hook that reads it did not fail (D4).
        fresh = poll and status.last_poll_at is not None and status.last_poll_at != self._seen_poll_at
        if fresh:
            self._seen_poll_at = status.last_poll_at
        try:
            base = self.base(session)
        except Exception:  # a link is a convenience: without an address there is none
            log.warning("push messages: the address for links could not be found", exc_info=True)
            base = None
        return ev.Context(
            now=now,
            poll=poll,
            status=status,
            filament_known=fresh and "filament" not in status.hook_errors,
            rights=self.rights(),
            base=base,
        )

    def _sending(self) -> bool:
        return self.task is not None and not self.task.done()

    def after_tick(self, poll: bool) -> None:
        """Engine listener: update the marks, then send what is due in a task of its own."""
        if self._stopped or self.update_started():
            return
        now = self.now()
        with self.sm.begin() as session:
            target = _target(session)
            if target is None:
                outgoing: list[ev.Outgoing] = []
            else:
                ctx = self._context(session, poll, now)
                cfg = self._detect(session, ctx)
                self._last_ctx = ctx
                outgoing = [] if self._sending() else ev.due(session, ctx, exclude=frozenset(), cfg=cfg)
        if target is None:
            self._set(problem=None)
            return
        if self._sending():
            self._again = True
            return
        check = poll and self.status.problem is not None and not outgoing
        if outgoing or check:
            self.task = asyncio.get_running_loop().create_task(
                self._run(target, outgoing, check), name="push-messages"
            )

    # ---- the sending task -----------------------------------------------------------------------

    async def _run(self, target: _Target, outgoing: list[ev.Outgoing], check: bool) -> None:
        tried: set[int] = set()
        try:
            while True:
                for out in outgoing:
                    tried.update(out.mark_ids)
                    # Root cause (final review 2): the outcomes of a round were written after its
                    # last message, so a stop, an update or a failed write after the first accepted
                    # message left it due, and the next process sent it again (Spec 10.6: "ein
                    # Neustart schickt nichts doppelt"). Fix: a print (events 1 and 2, never tried
                    # again) is marked before its call, every outcome is written right after it.
                    # Rejected: one write after the round with a `finally` - a cancelled task cannot
                    # write safely while a restore may be replacing the database.
                    if out.kind in ev.ONCE_PER_PRINT and not self._store([(out, "sending")], None, None):
                        return
                    outcome = await ha.send(self.http_factory, target.url, target.token, target.service, out.message)
                    if not self._store([(out, outcome)], None, self.now()):
                        return
                checked = await ha.ping(self.http_factory, target.url, target.token) if check else None
                if checked is not None and not self._store([], checked, self.now()):
                    return
                if not self._again or self._last_ctx is None:
                    return
                self._again = False
                if self.update_started():
                    return
                with self.sm.begin() as session:
                    next_target = _target(session)
                    outgoing = (
                        ev.due(session, self._last_ctx, exclude=frozenset(tried)) if next_target else []
                    )
                if next_target is None:
                    self._set(problem=None)
                    return
                target, check = next_target, False
                if not outgoing:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("push messages: sending failed")

    def _store(
        self, results: list[tuple[ev.Outgoing, str]], checked: str | None, now: datetime | None
    ) -> bool:
        """Write outcomes and take them into the status; synchronous, nothing runs in between.

        False: the task stops - an update or restore has begun (the database may be replaced, the
        marks stay as they are), or the messages were switched off meanwhile (final review 3: the
        warning stays off, Spec 10.6). `now` None: only the marks of prints before their call.
        """
        if self.update_started():
            return False
        with self.sm.begin() as session:
            for out, outcome in results:
                ev.record(session, out, outcome, now or self.now())
            switched_on = _target(session) is not None
        if not switched_on:
            self._set(problem=None)
            return False
        if now is not None:
            self._take(results, checked, now)
        return True

    def _take(self, results: list[tuple[ev.Outgoing, str]], checked: str | None, now: datetime) -> None:
        problem = self.status.problem
        last_at, last_outcome = self.status.last_at, self.status.last_outcome
        if results:
            failed = [outcome for _, outcome in results if outcome != "accepted"]
            last_at, last_outcome = now, (failed[0] if failed else "accepted")
            problem = failed[0] if failed else None
        if checked is not None:
            problem = None if checked == "accepted" else checked
        self._set(problem=problem, last_at=last_at, last_outcome=last_outcome)

    def _set(self, **changes: object) -> None:
        before = (self.status.last_at, self.status.last_outcome, self.status.problem)
        for name, value in changes.items():
            setattr(self.status, name, value)
        if (self.status.last_at, self.status.last_outcome, self.status.problem) != before and self.publish:
            try:
                self.publish(frozenset({TOPIC}))
            except Exception:  # a page that misses it catches up on reconnect
                log.warning("push messages: publishing the status failed", exc_info=True)

    async def send_test(self, url: str, token: str, service: str, message: ha.Message) -> str:
        """The test message of the settings page (Spec 10.6): with what is typed, nothing stored, no
        status touched. Here and not in the route, so that the web layer holds no HTTP client."""
        return await ha.send(self.http_factory, url, token, service, message)

    async def stop(self) -> None:
        """At shutdown: no new task, and a running one is ended and awaited."""
        self._stopped = True
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
