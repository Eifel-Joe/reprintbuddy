"""The printers' state in memory for the print plan (Spec 7.6 "Druckerzustand für den Druckplan", 0.4.2)."""

from datetime import datetime, timedelta

from reprintbuddy.bambuddy.types import Printer, PrinterStatus
from reprintbuddy.domain.printerboard import THROTTLE_SECONDS, TOPIC, PrinterBoard

T0 = datetime(2026, 11, 12, 10, 0)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Later:
    """Stands in for `loop.call_later`: keeps the calls, runs them on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, object]] = []

    def __call__(self, delay, callback):
        self.calls.append((delay, callback))

    def run(self) -> None:
        calls, self.calls = self.calls, []
        for _delay, callback in calls:
            callback()


def make(**kw):
    published: list[frozenset[str]] = []
    clock, later = Clock(), Later()
    board = PrinterBoard(published.append, now=clock, call_later=later, **kw)
    return board, published, clock, later


def status(pid=1, progress=10.0) -> PrinterStatus:
    return PrinterStatus(pid, True, "RUNNING", progress, 60, "Clip", False, False)


PRINTERS = [Printer(1, "Printer 1", "A1"), Printer(2, "Printer 2", "A1")]


def test_unknown_at_first():
    board, published, *_ = make()
    assert board.printers is None and board.statuses == {} and board.at is None
    assert board.shortest_first is False and published == []


def test_replace_publishes_at_once():
    board, published, *_ = make()
    board.replace(PRINTERS, {1: status()}, shortest_first=True)
    assert board.printers == tuple(PRINTERS)
    assert board.statuses == {1: status()}
    assert board.at == T0 and board.shortest_first is True
    assert published == [frozenset({TOPIC})]


def test_update_of_unknown_printer_is_ignored():
    board, published, *_ = make()
    board.update(status(1))  # nothing known yet
    board.replace(PRINTERS, {})
    published.clear()
    board.update(status(9))
    assert 9 not in board.statuses and published == []


def test_updates_publish_at_most_once_a_minute():
    board, published, clock, later = make()
    board.replace(PRINTERS, {1: status(progress=10.0)})
    published.clear()
    clock.now = T0 + timedelta(seconds=70)
    board.update(status(progress=11.0))  # more than a minute after the last: at once
    assert published == [frozenset({TOPIC})] and board.at == clock.now
    clock.now += timedelta(seconds=10)
    board.update(status(progress=12.0))
    board.update(status(progress=13.0))
    assert len(published) == 1  # held back
    assert [delay for delay, _ in later.calls] == [THROTTLE_SECONDS - 10]  # one call, not two
    assert board.statuses[1].progress == 13.0  # the state itself is current at once
    later.run()
    assert len(published) == 2


def test_unchanged_update_publishes_nothing():
    board, published, clock, later = make()
    board.replace(PRINTERS, {1: status()})
    published.clear()
    clock.now += timedelta(seconds=120)
    board.update(status())
    assert published == [] and later.calls == []
    assert board.at == clock.now  # but it is fresh


def test_forget_makes_state_unknown():
    board, published, *_ = make()
    board.replace(PRINTERS, {1: status()})
    published.clear()
    board.forget()
    assert board.printers is None and board.statuses == {} and board.at is None
    assert published == [frozenset({TOPIC})]
    board.forget()
    assert len(published) == 1  # nothing changed the second time


def test_without_a_loop_it_publishes_at_once():
    published: list = []
    board = PrinterBoard(published.append, now=Clock())
    board.replace(PRINTERS, {1: status()})
    board.update(status(progress=50.0))
    assert len(published) == 2


def test_without_publish_nothing_breaks():
    board = PrinterBoard(None)
    board.replace(PRINTERS, {1: status()})
    board.update(status(progress=50.0))
    board.forget()


class Handle:
    def __init__(self, later, callback):
        self.later, self.callback, self.cancelled = later, callback, False

    def cancel(self):
        self.cancelled = True


def test_a_query_cancels_a_held_back_publish():
    """update at 10 s, query at 20 s, update at 30 s: the publish held back at 10 s must not fire early for
    the update at 30 s - the next publish comes 60 s after the query (review v0.4.2, 9)."""
    published: list = []
    clock = Clock()
    handles: list[Handle] = []

    def later(delay, callback):
        handle = Handle(delay, callback)
        handles.append(handle)
        return handle

    board = PrinterBoard(published.append, now=clock, call_later=later)
    board.replace(PRINTERS, {1: status(progress=1.0)})
    clock.now = T0 + timedelta(seconds=10)
    board.update(status(progress=2.0))
    first = handles[-1]
    clock.now = T0 + timedelta(seconds=20)
    board.replace(PRINTERS, {1: status(progress=3.0)})
    assert first.cancelled
    clock.now = T0 + timedelta(seconds=30)
    board.update(status(progress=4.0))
    assert handles[-1] is not first and handles[-1].later == THROTTLE_SECONDS - 10
