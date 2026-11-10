"""`update_running`: the one question other parts (the write guard, later the background poll) ask."""

from datetime import UTC, datetime, timedelta

import pytest

from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.running import update_running
from reprintbuddy.ops.service import SYSTEMCTL, UPDATE_SERVICE
from reprintbuddy.ops.status import UpdateStatus, write_status

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
SHOW = (SYSTEMCTL, "show", "--property=ActiveState", "--value", UPDATE_SERVICE)


def put(tmp_path, state, minutes_ago, step="install"):
    write_status(tmp_path, UpdateStatus(state=state, step=step, started_at=NOW - timedelta(minutes=minutes_ago)))


def unit(fake_runner, answer):
    fake_runner.on(SHOW, CommandResult(0, answer + "\n"))


def test_idle_is_not_running_and_asks_nothing(tmp_path, fake_runner):
    assert update_running(tmp_path, fake_runner, NOW) is False
    assert fake_runner.calls == []


@pytest.mark.parametrize("state", ["succeeded", "failed", "rolled_back", "rollback_failed"])
def test_finished_states_are_not_running(tmp_path, fake_runner, state):
    put(tmp_path, state, 1)
    assert update_running(tmp_path, fake_runner, NOW) is False
    assert fake_runner.calls == []


@pytest.mark.parametrize("answer", ["activating", "active", "deactivating", "reloading"])
def test_a_live_unit_means_running(tmp_path, fake_runner, answer):
    put(tmp_path, "running", 5)
    unit(fake_runner, answer)
    assert update_running(tmp_path, fake_runner, NOW) is True


@pytest.mark.parametrize("answer", ["inactive", "failed"])
def test_a_dead_unit_means_the_state_is_stale(tmp_path, fake_runner, answer):
    put(tmp_path, "running", 5)
    unit(fake_runner, answer)
    assert update_running(tmp_path, fake_runner, NOW) is False


def test_unknown_unit_state_counts_as_running(tmp_path, fake_runner):
    put(tmp_path, "running", 5)
    fake_runner.on(SHOW, CommandResult(127, "", "not found"))
    assert update_running(tmp_path, fake_runner, NOW) is True


def test_freshly_queued_counts_before_the_unit_is_active(tmp_path, fake_runner):
    write_status(tmp_path, UpdateStatus(state="queued", step="start", started_at=NOW - timedelta(seconds=3)))
    unit(fake_runner, "inactive")
    assert update_running(tmp_path, fake_runner, NOW) is True
    assert fake_runner.calls == []  # no need to ask yet


def test_an_old_queued_state_with_a_dead_unit_is_stale(tmp_path, fake_runner):
    put(tmp_path, "queued", 2, step="start")
    unit(fake_runner, "failed")
    assert update_running(tmp_path, fake_runner, NOW) is False


def test_a_state_older_than_the_limit_is_stale_without_asking(tmp_path, fake_runner):
    put(tmp_path, "running", 45)
    assert update_running(tmp_path, fake_runner, NOW) is False
    assert fake_runner.calls == []


def test_just_started_is_a_cheap_look_at_the_state_file_only(tmp_path, fake_runner):
    from reprintbuddy.ops.running import update_just_started

    assert update_just_started(tmp_path, NOW) is False  # nothing written
    put(tmp_path, "queued", 0)
    assert update_just_started(tmp_path, NOW) is True
    put(tmp_path, "running", 0)
    assert update_just_started(tmp_path, NOW) is True
    assert fake_runner.calls == []  # never asks systemd


def test_an_update_older_than_the_grace_is_not_just_started(tmp_path):
    from reprintbuddy.ops.running import update_just_started

    put(tmp_path, "running", 5)  # five minutes: the slow question decides about that one
    assert update_just_started(tmp_path, NOW) is False


@pytest.mark.parametrize("state", ["succeeded", "failed", "rolled_back", "rollback_failed", "idle"])
def test_a_finished_update_is_not_just_started(tmp_path, state):
    from reprintbuddy.ops.running import update_just_started

    put(tmp_path, state, 0)
    assert update_just_started(tmp_path, NOW) is False


def test_an_update_started_by_hand_counts_while_it_holds_the_lock(tmp_path, fake_runner):
    """p91: `reprintbuddy update` typed in the container runs outside the unit; its lock tells."""
    from reprintbuddy.ops import oplock

    put(tmp_path, "running", 5)
    unit(fake_runner, "inactive")
    held = oplock.acquire(tmp_path, "update")
    try:
        assert update_running(tmp_path, fake_runner, NOW) is True
    finally:
        held.release()
    assert update_running(tmp_path, fake_runner, NOW) is False


def test_a_restore_lock_is_no_update(tmp_path, fake_runner):
    from reprintbuddy.ops import oplock

    put(tmp_path, "running", 5)
    unit(fake_runner, "inactive")
    held = oplock.acquire(tmp_path, "restore")
    try:
        assert update_running(tmp_path, fake_runner, NOW) is False
    finally:
        held.release()
