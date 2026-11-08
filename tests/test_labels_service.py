"""The whole print of labels with its checks (Spec 8.5): ask the printer once, then one job per
label, never again by itself; say why nothing was printed or how far it got."""

import socket
import threading

import pytest
from PIL import Image

from reprintbuddy.labels import brother, service, snmp
from reprintbuddy.labels.errors import PrinterAddressRefused, PrinterUnreachable, SendAborted
from reprintbuddy.labels.rolls import DK_11209
from reprintbuddy.labels.status import Problem

READY = bytes.fromhex("80204234413004000000" "3e0b" "0000030000" "1d" "00000000000000" "01" "0000000000" "00")


def block(**changes):
    data = bytearray(READY)
    for index, value in changes.items():
        data[int(index[1:])] = value
    return bytes(data)


class Recorder:
    """Takes the place of the network: what the printer answers to the status question and what
    happens to every job that is sent."""

    def __init__(self, monkeypatch, *, answer=READY, outcomes=()):
        self.asked, self.sent = [], []
        self.answer, self.outcomes = answer, list(outcomes)
        monkeypatch.setattr(snmp, "query_status", self.ask)
        monkeypatch.setattr(brother, "send_job", self.send)

    def ask(self, host, **kwargs):
        self.asked.append((host, kwargs))
        return self.answer

    def send(self, host, data, **kwargs):
        self.sent.append((host, data, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome


@pytest.fixture
def net(monkeypatch):
    return Recorder(monkeypatch)


IMAGE = Image.new("1", (696, 271), 1)


def run(**kwargs):
    kwargs.setdefault("allow_local", True)
    return service.print_labels("127.0.0.1", DK_11209, IMAGE, kwargs.pop("copies", 1), **kwargs)


def test_a_ready_printer_gets_one_job_per_label(net):
    outcome = run(copies=3)
    assert (outcome.printed, outcome.requested, outcome.problem, outcome.error) == (3, 3, None, None)
    assert len(net.sent) == 3 and len({data for _, data, _ in net.sent}) == 1
    assert net.sent[0][1] == brother.build_job(IMAGE, DK_11209)


def test_the_printer_is_asked_once_before_the_first_label_not_before_each(net):
    run(copies=4)
    assert len(net.asked) == 1


def test_the_questions_and_jobs_use_the_limits_of_the_spec(net):
    run()
    assert net.asked[0][1].get("timeout", snmp.TIMEOUT) == 1.0
    assert snmp.TIMEOUT == 1.0 and brother.CONNECT_TIMEOUT == 3.0 and brother.WAIT_TIMEOUT == 10.0


@pytest.mark.parametrize(
    "changes,key",
    [({"b9": 0x10}, "cover_open"), ({"b8": 0x02}, "roll_empty"), ({"b10": 0, "b11": 0, "b17": 0}, "no_roll"), ({"b17": 100}, "wrong_roll")],
)
def test_a_problem_of_the_printer_stops_everything_and_nothing_is_sent(monkeypatch, changes, key):
    net = Recorder(monkeypatch, answer=block(**changes))
    outcome = run(copies=2)
    assert outcome.problem is not None and outcome.problem.key == key
    assert (outcome.printed, outcome.requested, outcome.error) == (0, 2, None)
    assert net.sent == []


def test_no_answer_from_the_printer_means_print_anyway(monkeypatch):
    net = Recorder(monkeypatch, answer=None)
    outcome = run(copies=2)
    assert outcome.printed == 2 and outcome.problem is None
    assert len(net.sent) == 2


def test_an_answer_that_is_not_a_status_block_means_print_anyway(monkeypatch):
    net = Recorder(monkeypatch, answer=b"\x00" * 32)
    assert run().printed == 1 and len(net.sent) == 1


def test_a_failing_job_stops_the_rest_and_says_how_far_it_got(monkeypatch):
    net = Recorder(monkeypatch, outcomes=[None, SendAborted()])
    outcome = run(copies=5)
    assert (outcome.printed, outcome.requested) == (1, 5)
    assert isinstance(outcome.error, SendAborted)
    assert len(net.sent) == 2  # no third, and the failed one is not sent again


def test_an_unreachable_printer_prints_nothing(monkeypatch):
    net = Recorder(monkeypatch, outcomes=[PrinterUnreachable()])
    outcome = run(copies=2)
    assert (outcome.printed, outcome.requested) == (0, 2)
    assert isinstance(outcome.error, PrinterUnreachable)
    assert len(net.sent) == 1


def test_this_machine_is_refused_before_anything_is_asked(net):
    outcome = service.print_labels("127.0.0.1", DK_11209, IMAGE, 1)
    assert isinstance(outcome.error, PrinterAddressRefused)
    assert net.asked == [] and net.sent == []


def test_the_time_for_a_long_series_is_limited(monkeypatch):
    net = Recorder(monkeypatch)
    now = [0.0]

    def clock():
        now[0] += 20.0  # every look at the clock is 20 seconds later
        return now[0]

    outcome = run(copies=99, clock=clock, deadline=100)
    assert 0 < outcome.printed < 99
    assert outcome.error is not None and outcome.error.key == "labels.err.deadline"
    assert len(net.sent) == outcome.printed


def test_a_second_print_at_the_same_time_is_told_the_printer_is_busy(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_send(host, data, **kwargs):
        started.set()
        release.wait(5)

    monkeypatch.setattr(snmp, "query_status", lambda host, **kw: READY)
    monkeypatch.setattr(brother, "send_job", slow_send)
    first = threading.Thread(target=lambda: run())
    first.start()
    assert started.wait(5)
    second = run(lock_wait=0.1)
    release.set()
    first.join(5)
    assert second.printed == 0 and second.error is not None and second.error.key == "labels.err.busy"


def test_the_lock_is_free_again_after_a_failure(monkeypatch):
    Recorder(monkeypatch, outcomes=[SendAborted()])
    run()
    assert run().printed == 1


def test_copies_must_be_a_sensible_number():
    for bad in (0, -1, 100, 1.5, "2", None, True):
        with pytest.raises(ValueError):
            service.print_labels("192.0.2.9", DK_11209, IMAGE, bad)
    assert service.MAX_COPIES == 99


def test_the_job_is_built_once_for_all_copies(monkeypatch):
    calls = []
    real = brother.build_job
    monkeypatch.setattr(brother, "build_job", lambda *a: calls.append(1) or real(*a))
    Recorder(monkeypatch)
    run(copies=3)
    assert len(calls) == 1
