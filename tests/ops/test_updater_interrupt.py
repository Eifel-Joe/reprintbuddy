"""p610: an update or a way back stopped from outside is rolled back like a failure (Spec 11.3a).

systemd stops the helper with SIGTERM when its time runs out, when the unit is stopped and on shutdown; after
`TimeoutStopSec` (90 s by default) SIGKILL follows. Up to 1.2.0-beta.1 the handler only wrote the final state and
ended the process: the rollback never ran, and a stopped service stayed stopped. The signal is sent here through
`InterruptGuard.signal`, the function the handler calls, so the tests run the same on Windows and Linux.
"""

import signal
from datetime import UTC, datetime, timedelta

import pytest

from reprintbuddy.ops.status import UpdateStatus, read_status, write_status
from reprintbuddy.ops.updater import InterruptGuard, Interrupted, interrupt_handler

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def guard_for(tmp_path):
    db = tmp_path / "u.db"
    db.write_bytes(b"")
    return InterruptGuard(db, now=lambda: NOW + timedelta(minutes=30))


# --- the guard ----------------------------------------------------------------------------------------------


def test_idle_writes_the_final_state_and_exits_as_before(tmp_path):
    """Before the lock and outside a run nothing can be rolled back: the old behaviour."""
    guard = guard_for(tmp_path)
    write_status(tmp_path, UpdateStatus(state="running", step="fetch", started_at=NOW))
    with pytest.raises(SystemExit) as stop:
        guard.signal(signal.SIGTERM)
    assert stop.value.code == 128 + signal.SIGTERM
    assert read_status(tmp_path).reason == "interrupted_untouched"


def test_armed_raises_so_that_the_rollback_can_run(tmp_path):
    guard = guard_for(tmp_path)
    guard.arm()
    with pytest.raises(Interrupted):
        guard.signal(signal.SIGTERM)
    assert guard.pending
    assert read_status(tmp_path).state == "idle"  # the run writes its own final state


def test_interrupted_is_no_exception_so_no_except_exception_swallows_it():
    assert not issubclass(Interrupted, Exception)
    assert issubclass(Interrupted, BaseException)


def test_holding_remembers_the_signal_and_returns(tmp_path):
    """While the rollback runs, a signal must not cut it off: the service would stay stopped."""
    guard = guard_for(tmp_path)
    guard.hold()
    guard.signal(signal.SIGTERM)  # no exception, no exit
    assert guard.pending


def test_the_first_signal_switches_to_holding(tmp_path):
    """A second SIGTERM (systemd sends one to every process of the unit) must not cut off the rollback."""
    guard = guard_for(tmp_path)
    guard.arm()
    with pytest.raises(Interrupted):
        guard.signal(signal.SIGTERM)
    guard.signal(signal.SIGTERM)  # returns
    assert guard.signals == 2


def test_arming_after_a_signal_while_holding_raises_at_once(tmp_path):
    """A signal between two armed parts is not lost."""
    guard = guard_for(tmp_path)
    guard.hold()
    guard.signal(signal.SIGTERM)
    with pytest.raises(Interrupted):
        guard.arm()


def test_command_times_are_short_once_a_signal_came(tmp_path):
    """systemd sends SIGKILL 90 s after SIGTERM: the rollback has to fit in."""
    guard = guard_for(tmp_path)
    assert guard.timeout(120.0) == 120.0
    guard.hold()
    guard.signal(signal.SIGTERM)
    assert guard.timeout(120.0) == 20.0
    assert guard.timeout(5.0) == 5.0


def test_the_handler_hands_the_signal_to_the_guard(tmp_path):
    guard = guard_for(tmp_path)
    handler = interrupt_handler(tmp_path / "u.db", now=lambda: NOW, guard=guard)
    guard.arm()
    with pytest.raises(Interrupted):
        handler(signal.SIGTERM, None)


# --- the command line -----------------------------------------------------------------------------------------


def test_the_command_line_gives_the_guard_of_its_handler_to_the_update(tmp_path, monkeypatch):
    from reprintbuddy import cli

    db = tmp_path / "u.db"
    assert cli.main(["migrate", "--db", str(db)]) == 0
    handlers = {}
    monkeypatch.setattr(cli.signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler))
    waiters = []

    def waiter(db_path, **kwargs):
        waiters.append(kwargs)
        return lambda: True

    monkeypatch.setattr(cli, "http_health_waiter", waiter)
    seen = {}

    def fake(db_path, **kwargs):
        guard = kwargs["guard"]
        guard.arm()
        with pytest.raises(Interrupted):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        seen["hurried"] = waiters[0]["hurry"]()
        return UpdateStatus(state="rolled_back", step="rollback")

    monkeypatch.setattr(cli, "run_update", fake)
    assert cli.main(["update", "--db", str(db)]) == 1
    assert seen == {"hurried": True}  # the wait for the answer is cut short too
