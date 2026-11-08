"""The print of one or several labels, with the checks of Spec 8.5.

Before the first label the printer is asked about its roll and condition (SNMP, at most one second).
Not ready (cover open, roll empty or missing, wrong roll, error): nothing is sent and the reason is
returned. No answer: print anyway. Then one job per label, one after the other, never sent a second
time by itself; if one fails the rest is not sent and the outcome says how far it got ("x of n").
One print at a time (a lock): two people pressing at once do not mix their jobs on the one printer,
the second is told the printer is busy. A series has a total time limit.

This is blocking network code: the web layer runs it in a worker thread.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from PIL import Image

from reprintbuddy.labels import brother, snmp, status
from reprintbuddy.labels.addresses import resolve
from reprintbuddy.labels.errors import LabelError
from reprintbuddy.labels.rolls import Roll

MAX_COPIES = 99
SERIES_SECONDS = 150.0  # all labels of one press together
LOCK_WAIT = 1.0
_LOCK = threading.Lock()


class PrinterBusy(LabelError):
    """Another print is running right now."""

    key = "labels.err.busy"


class SeriesTooLong(LabelError):
    """The time for the whole series ran out; what was printed so far is in the outcome."""

    key = "labels.err.deadline"


@dataclass(frozen=True)
class Outcome:
    printed: int  # labels handed over to the printer
    requested: int
    problem: status.Problem | None = None  # the printer is not ready: nothing was sent
    error: LabelError | None = None  # the way to the printer failed

    @property
    def ok(self) -> bool:
        return self.problem is None and self.error is None and self.printed == self.requested


def print_labels(
    host: str,
    roll: Roll,
    image: Image.Image,
    copies: int,
    *,
    allow_local: bool = False,
    clock=time.monotonic,
    deadline: float = SERIES_SECONDS,
    lock_wait: float = LOCK_WAIT,
) -> Outcome:
    """Print `copies` labels of `image` on the printer at `host` (see the module text).

    `allow_local` and `clock` exist for the tests; the web layer never sets them."""
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= MAX_COPIES:
        raise ValueError(f"copies must be a whole number from 1 to {MAX_COPIES}")
    try:  # looked up before the lock is taken: a slow name server must not make others wait
        address = resolve(host, allow_local=allow_local)
    except LabelError as error:
        return Outcome(0, copies, error=error)
    if not _LOCK.acquire(timeout=lock_wait):
        return Outcome(0, copies, error=PrinterBusy())
    try:
        state = status.parse(snmp.query_status(address, allow_local=allow_local))
        if state is not None:
            problem = status.problem(state, roll)
            if problem is not None:
                return Outcome(0, copies, problem=problem)
        job = brother.build_job(image, roll)
        started = clock()
        for printed in range(copies):
            if printed and clock() - started > deadline:
                return Outcome(printed, copies, error=SeriesTooLong())
            try:
                brother.send_job(address, job, allow_local=allow_local)
            except LabelError as error:
                return Outcome(printed, copies, error=error)
        return Outcome(copies, copies)
    finally:
        _LOCK.release()
