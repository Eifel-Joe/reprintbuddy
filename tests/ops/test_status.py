import os
from datetime import UTC, datetime, timedelta

import pytest

from reprintbuddy.ops.status import (
    STATUS_FILENAME,
    UPDATE_STEPS,
    UpdateStatus,
    is_busy,
    read_status,
    status_path,
    write_status,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def test_read_missing_is_idle(tmp_path):
    assert read_status(tmp_path) == UpdateStatus()
    assert UpdateStatus().state == "idle"


def test_read_corrupt_is_idle(tmp_path):
    status_path(tmp_path).write_text("{nope", encoding="utf-8")
    assert read_status(tmp_path) == UpdateStatus()


def test_read_wrong_shape_is_idle(tmp_path):
    status_path(tmp_path).write_text('["a"]', encoding="utf-8")
    assert read_status(tmp_path) == UpdateStatus()
    status_path(tmp_path).write_text('{"state": "exploding"}', encoding="utf-8")
    assert read_status(tmp_path) == UpdateStatus()


def test_status_path(tmp_path):
    assert status_path(tmp_path) == tmp_path / STATUS_FILENAME == tmp_path / "update-status.json"


def test_steps_are_the_known_ones():
    assert UPDATE_STEPS == (
        "start", "backup", "fetch", "checkout", "install", "verify", "migrate",
        "restart", "health", "rollback", "done",
    )


def test_roundtrip(tmp_path):
    status = UpdateStatus(
        state="rolled_back",
        step="install",
        from_tag="v0.1.0",
        to_tag="v0.2.0",
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=2),
        message="install: boom",
        backup="/x/backups/b.db",
    )
    write_status(tmp_path, status)
    assert read_status(tmp_path) == status
    assert status.to_json()["started_at"] == "2026-11-05T12:00:00+00:00"


def test_write_leaves_no_tmp(tmp_path):
    write_status(tmp_path, UpdateStatus(state="running", step="backup"))
    assert list(tmp_path.glob("*.tmp")) == []
    assert [p.name for p in tmp_path.iterdir()] == [STATUS_FILENAME]
    if os.name == "posix":
        assert (status_path(tmp_path).stat().st_mode & 0o777) == 0o600


def test_status_json_holds_no_secret_fields(tmp_path):
    assert set(UpdateStatus().to_json()) == {
        "state", "step", "from_tag", "to_tag", "started_at", "finished_at", "message", "backup",
    }


@pytest.mark.parametrize(
    "state,age_minutes,expected",
    [
        ("queued", 1, True),
        ("running", 29, True),
        ("running", 31, False),
        ("succeeded", 1, False),
        ("failed", 1, False),
    ],
)
def test_is_busy(state, age_minutes, expected):
    status = UpdateStatus(state=state, started_at=NOW - timedelta(minutes=age_minutes))
    assert is_busy(status, NOW) is expected


def test_running_without_start_time_is_not_busy():
    assert not is_busy(UpdateStatus(state="running"), NOW)
