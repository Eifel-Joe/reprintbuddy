"""The printers' state for the print plan, in memory only (Spec 7.6 "Druckerzustand für den Druckplan", 0.4.2).

Filled by every regular query (one request for all printers, `replace`) and, in between, by the messages
`printer_status` of Bambuddy's live channel (`update`). Only for display: it never causes a booking, a
hand-over, a pause or a warning, and it is not stored (a restart starts unknown until the first query).

Open pages follow it as topic "printers" (Spec 10.5). A query publishes at once; live messages come with
every percent and every temperature (Bambuddy `main.py:2425-2554`), so they publish at most once a minute.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from reprintbuddy.bambuddy.types import Printer, PrinterStatus
from reprintbuddy.models import utcnow

log = logging.getLogger(__name__)

TOPIC = "printers"
THROTTLE_SECONDS = 60.0  # "höchstens etwa einmal je Minute" (Spec 7.6)

Publish = Callable[[frozenset[str]], None]
CallLater = Callable[[float, Callable[[], None]], object]


def _loop_call_later(delay: float, callback: Callable[[], None]) -> object | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.call_later(delay, callback)


class PrinterBoard:
    def __init__(
        self,
        publish: Publish | None,
        *,
        now: Callable[[], datetime] | None = None,
        call_later: CallLater | None = None,
    ) -> None:
        self._publish = publish
        # Looked up at each call, not bound at import: a fixed clock set later (the picture tool) holds here too.
        self._now = now or (lambda: utcnow())
        self._call_later = call_later
        self.printers: tuple[Printer, ...] | None = None  # None: unknown
        self.statuses: dict[int, PrinterStatus] = {}
        self.at: datetime | None = None  # the last answer, from a query or a live message
        self.shortest_first = False  # Bambuddy's "shortest job first", read with the query
        self._published_at: datetime | None = None
        self._pending = False

    # ---- writes ---------------------------------------------------------------------------

    def replace(
        self, printers: Sequence[Printer], statuses: Mapping[int, PrinterStatus], *, shortest_first: bool = False
    ) -> None:
        """A regular query answered: the whole state, published at once."""
        self.printers = tuple(printers)
        self.statuses = dict(statuses)
        self.shortest_first = shortest_first
        self.at = self._now()
        self._send()

    def forget(self) -> None:
        """The state is not known (Bambuddy unreachable, the read refused)."""
        if self.printers is None and not self.statuses and self.at is None:
            return
        self.printers = None
        self.statuses = {}
        self.at = None
        self._send()

    def update(self, status: PrinterStatus) -> None:
        """One printer's state from the live channel; only for a printer the last query listed."""
        if self.printers is None or all(p.id != status.printer_id for p in self.printers):
            return
        changed = self.statuses.get(status.printer_id) != status
        self.statuses[status.printer_id] = status
        self.at = self._now()
        if not changed:
            return
        elapsed = None if self._published_at is None else (self.at - self._published_at).total_seconds()
        if elapsed is None or elapsed >= THROTTLE_SECONDS:
            self._send()
            return
        if self._pending:
            return
        self._pending = True
        schedule = self._call_later or _loop_call_later
        if schedule(THROTTLE_SECONDS - elapsed, self._flush) is None and self._call_later is None:
            self._flush()  # no running loop (a script, a test without background tasks): at once

    # ---- publishing -----------------------------------------------------------------------

    def _flush(self) -> None:
        if self._pending:
            self._send()

    def _send(self) -> None:
        self._pending = False
        self._published_at = self._now()
        if self._publish is None:
            return
        try:
            self._publish(frozenset({TOPIC}))
        except Exception:  # a page that misses it catches up on reconnect
            log.warning("printer board: publishing failed", exc_info=True)
