"""The writer of the activity log (Spec 10.7 "nebenher"): queue in memory, written in one run;
failed sign-ins combined; changes of a state only; 90 days; nothing ever held up."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from reprintbuddy.activity import states
from reprintbuddy.activity.writer import MAX_PENDING, ActivityLog, read_restored_marker, write_restored_marker
from reprintbuddy.models import ActivityEntry, ActivityMark
from reprintbuddy.ops.status import UpdateStatus, mark_interrupted, write_status

T0 = datetime(2026, 11, 9, 12, 0)


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def rows(db) -> list[ActivityEntry]:
    with db() as s:
        return list(s.scalars(select(ActivityEntry).order_by(ActivityEntry.id)))


def test_record_queues_and_drain_writes(db):
    activity = ActivityLog(db)
    activity.record("login", "user", address="192.0.2.7")
    activity.record("logout", "user", address="192.0.2.7")
    assert rows(db) == [] and activity.pending == 2
    assert activity.drain() == 2
    assert [(r.area, r.kind, r.actor, r.address) for r in rows(db)] == [
        ("login", "login", "user", "192.0.2.7"),
        ("login", "logout", "user", "192.0.2.7"),
    ]
    assert activity.pending == 0


def test_an_unknown_kind_is_logged_not_raised(db, caplog):
    activity = ActivityLog(db)
    with caplog.at_level(logging.ERROR):
        activity.record("no_such_event", "user")
    assert activity.pending == 0
    assert "activity log" in caplog.text


def test_failed_sign_ins_of_one_address_within_ten_minutes_are_one_line(db):
    clock = Clock()
    activity = ActivityLog(db, now=clock)
    for minutes in (0, 4, 9):
        clock.at = T0 + timedelta(minutes=minutes)
        activity.record("login_failed", "user", address="192.0.2.7")
        activity.drain()
    clock.at = T0 + timedelta(minutes=10)
    activity.record("login_failed", "user", address="198.51.100.1")  # another device: its own line
    activity.drain()
    clock.at = T0 + timedelta(minutes=25)  # 16 minutes after the last one: a new line
    activity.record("login_failed", "user", address="192.0.2.7")
    activity.drain()
    found = [(r.address, r.at, r.details["count"]) for r in rows(db)]
    assert found == [
        ("192.0.2.7", T0 + timedelta(minutes=9), 3),  # stands at its last attempt
        ("198.51.100.1", T0 + timedelta(minutes=10), 1),
        ("192.0.2.7", T0 + timedelta(minutes=25), 1),
    ]


def test_failed_sign_ins_in_one_run_are_combined_too(db):
    activity = ActivityLog(db, now=Clock())
    for _ in range(5):
        activity.record("login_failed", "user", address="192.0.2.7")
    activity.drain()
    [line] = rows(db)
    assert line.details["count"] == 5


def test_a_transition_writes_only_changes_and_survives_a_restart(db):
    activity = ActivityLog(db)
    for state in ("ok", "ok", "unreachable", "unreachable", "refused", "ok"):
        activity.transition("bambuddy.connection", state, "bambuddy_connection", "bambuddy", {"state": state})
        activity.drain()
    assert [r.details["state"] for r in rows(db)] == ["unreachable", "refused", "ok"]
    restarted = ActivityLog(db)  # the state is in the database, not in memory
    restarted.transition("bambuddy.connection", "ok", "bambuddy_connection", "bambuddy", {"state": "ok"})
    restarted.drain()
    assert len(rows(db)) == 3
    with db() as s:
        assert s.get(ActivityMark, "bambuddy.connection").state == "ok"


def test_a_first_bad_state_is_a_change(db):
    activity = ActivityLog(db)
    activity.transition("bambuddy.rights", "missing", "bambuddy_rights", "bambuddy", {"state": "missing"})
    activity.drain()
    assert [r.details for r in rows(db)] == [{"state": "missing"}]


def test_lines_older_than_90_days_are_deleted(db):
    clock = Clock(T0 - timedelta(days=91))
    activity = ActivityLog(db, now=clock)
    activity.record("login", "user")
    clock.at = T0 - timedelta(days=89)
    activity.record("logout", "user")
    activity.drain()
    clock.at = T0
    activity.prune()
    assert [r.kind for r in rows(db)] == ["logout"]


def test_a_failing_write_holds_nothing_up(db, caplog, monkeypatch):
    activity = ActivityLog(db)
    activity.record("login", "user")

    def broken():
        raise OSError("disk full")

    monkeypatch.setattr(activity.sm, "begin", broken)
    with caplog.at_level(logging.WARNING):
        assert activity.drain() == 1  # taken, not written, no exception
    assert "could not be written" in caplog.text
    assert activity.pending == 0


def test_the_queue_is_bounded(db):
    activity = ActivityLog(db)
    for _ in range(MAX_PENDING + 50):
        activity.record("login", "user")
    assert activity.pending == MAX_PENDING


async def test_nothing_is_written_while_a_restore_or_update_runs(db):
    busy = True
    activity = ActivityLog(db, may_write=lambda: not busy)
    activity.record("login", "user")
    assert await activity.drain_counted() is False
    assert rows(db) == [] and activity.pending == 1  # kept for later
    busy = False
    assert await activity.drain_counted() is True
    assert len(rows(db)) == 1


async def test_a_run_counts_as_a_write_in_flight_until_it_is_written(db):
    """The restore waits while `writes_in_flight` is above its own request (web/routes/backup.py):
    a run that is in its worker thread is counted, so the restore waits for it."""
    in_flight = 0
    restore_active = False
    entered, release = threading.Event(), threading.Event()

    def begin():
        nonlocal in_flight
        in_flight += 1

    def end():
        nonlocal in_flight
        in_flight -= 1

    activity = ActivityLog(db, may_write=lambda: not restore_active, begin_write=begin, end_write=end)
    real_drain = activity.drain

    def slow_drain() -> int:
        entered.set()
        release.wait(5)
        return real_drain()

    activity.drain = slow_drain  # type: ignore[method-assign]
    activity.record("login", "user")
    task = asyncio.create_task(activity.drain_counted())
    while not entered.is_set():
        await asyncio.sleep(0.01)
    # A restore begins now: it sees the run and waits for it.
    restore_active = True
    assert in_flight == 1
    release.set()
    assert await task is True
    assert in_flight == 0
    assert len(rows(db)) == 1  # written before the database could be replaced
    # Afterwards nothing new is written while the restore runs.
    activity.record("logout", "user")
    assert await activity.drain_counted() is False


def test_the_result_of_an_update_once_per_update(db, tmp_path):
    def outcome():
        from reprintbuddy.ops.status import read_status

        return states.update_outcome(read_status(tmp_path))

    activity = ActivityLog(db, update_result=outcome)
    activity.check_update()
    activity.drain()
    assert rows(db) == []  # no update has run
    started = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    write_status(tmp_path, UpdateStatus(state="succeeded", from_tag="v0.3.12", to_tag="v0.3.13",
                                        started_at=started, message="pip: lots of text"))
    for _ in range(3):  # read every minute and at every start
        activity.check_update()
        activity.drain()
    start, result = rows(db)
    assert (start.kind, start.actor, start.at) == ("update_started", "user", started.replace(tzinfo=None))
    assert start.details == {"from": "v0.3.12", "to": "v0.3.13"}
    assert (result.kind, result.actor) == ("update_result", "system")
    assert result.details == {"from": "v0.3.12", "to": "v0.3.13", "result": "done"}


@pytest.mark.parametrize(
    ("state", "result"),
    [("succeeded", "done"), ("failed", "failed"), ("rolled_back", "rolled_back"), ("rollback_failed", "rollback_failed")],
)
def test_update_results(state, result):
    status = UpdateStatus(state=state, started_at=datetime(2026, 11, 9, tzinfo=UTC), message="git: something")
    assert states.update_outcome(status)["result"] == result


@pytest.mark.parametrize("step", ["fetch", "install"])
def test_an_interrupted_update_is_named_so(tmp_path, step):
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    write_status(tmp_path, UpdateStatus(state="running", step=step, started_at=now))
    final = mark_interrupted(tmp_path, now)
    assert states.update_outcome(final)["result"] == "interrupted"


def test_a_running_update_has_no_result_yet():
    assert states.update_outcome(UpdateStatus(state="running", started_at=datetime(2026, 11, 9, tzinfo=UTC))) is None
    assert states.update_outcome(UpdateStatus()) is None


def test_the_restore_marker_is_read_once(tmp_path):
    write_restored_marker(tmp_path, created_at=datetime(2026, 11, 1, 8, 0, tzinfo=UTC), version="0.3.12", via=None)
    assert read_restored_marker(tmp_path) == {
        "created_at": "2026-11-01T08:00:00+00:00", "version": "0.3.12", "via": None,
    }
    assert read_restored_marker(tmp_path) is None


class _Status:
    def __init__(self, configured=True, reachable=True, poll_error=None, rejected=None):
        self.configured, self.reachable, self.poll_error, self.rejected = configured, reachable, poll_error, rejected


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (_Status(configured=False), None),
        (_Status(reachable=None), None),
        (_Status(reachable=False), "unreachable"),
        (_Status(poll_error="forbidden"), "refused"),
        (_Status(rejected="queue"), "ok"),  # a refused hand-over of a print is the journal's, not the poll's
        (_Status(), "ok"),
    ],
)
def test_the_connection_state(status, state):
    assert states.connection_state(status) == state




def test_failed_sign_ins_keep_a_fixed_window_and_sort_by_the_last(db):
    """Review finding 6: the window starts with the first attempt; the line stands at its last one."""
    clock = Clock()
    activity = ActivityLog(db, now=clock)
    for minutes in (0, 6, 9, 12):  # 12 is outside the 10 minutes after the first
        clock.at = T0 + timedelta(minutes=minutes)
        activity.record("login_failed", "user", address="192.0.2.7")
        activity.drain()
    first, second = rows(db)
    assert first.details == {"count": 3, "first": T0.isoformat(), "last": (T0 + timedelta(minutes=9)).isoformat()}
    assert first.at == T0 + timedelta(minutes=9)
    assert second.details["count"] == 1 and second.at == T0 + timedelta(minutes=12)


def test_one_bad_event_does_not_take_the_others_with_it(db, caplog):
    """Review finding 8."""
    activity = ActivityLog(db)
    activity.record("login", "user")
    activity.record("logout", "user", {"bad": object()})  # cannot be stored as JSON
    activity.record("login", "user")
    with caplog.at_level(logging.WARNING):
        activity.drain()
    assert [r.kind for r in rows(db)] == ["login", "login"]


async def test_a_cancelled_run_stays_counted_until_its_thread_is_done(db):
    """Review finding 9: the count drops when the write is done, not when the waiting task is cancelled."""
    in_flight = 0
    entered, release = threading.Event(), threading.Event()

    def begin():
        nonlocal in_flight
        in_flight += 1

    def end():
        nonlocal in_flight
        in_flight -= 1

    activity = ActivityLog(db, begin_write=begin, end_write=end)
    real = activity.drain

    def slow() -> int:
        entered.set()
        release.wait(5)
        return real()

    activity.drain = slow
    activity.record("login", "user")
    task = asyncio.create_task(activity.drain_counted())
    while not entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert in_flight == 1  # the thread still writes
    release.set()
    for _ in range(200):
        if in_flight == 0:
            break
        await asyncio.sleep(0.01)
    assert in_flight == 0
