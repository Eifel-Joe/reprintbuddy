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
        "reason", "params", "acknowledged",
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


# v0.4.9: "checkout" unpacks the package beside the installed version; the installation is still untouched.
@pytest.mark.parametrize(
    "step,state",
    [("backup", "failed"), ("fetch", "failed"), ("checkout", "failed"), ("install", "rollback_failed"), ("rollback", "rollback_failed")],
)
def test_interrupted_update_leaves_a_final_state(tmp_path, step, state):
    from reprintbuddy.ops.status import mark_interrupted

    write_status(
        tmp_path,
        UpdateStatus(state="running", step=step, from_tag="v0.1.0", to_tag="v0.2.0", started_at=NOW, backup="/b/x.db"),
    )
    result = mark_interrupted(tmp_path, NOW + timedelta(minutes=30))
    assert result is not None and result.state == state
    assert result.step == step
    assert f"{step}: interrupted" in result.message
    assert result.reason == ("interrupted_untouched" if state == "failed" else "interrupted_partial")  # p176
    assert result.finished_at == NOW + timedelta(minutes=30)
    assert read_status(tmp_path) == result
    if state == "rollback_failed":
        assert "/b/x.db" in result.message


def test_interrupt_leaves_finished_states_alone(tmp_path):
    from reprintbuddy.ops.status import mark_interrupted

    done = UpdateStatus(state="succeeded", step="done", started_at=NOW, finished_at=NOW)
    write_status(tmp_path, done)
    assert mark_interrupted(tmp_path, NOW) is None
    assert read_status(tmp_path) == done


def test_reason_and_params_survive_the_file(tmp_path):  # v0.3.14 p176
    status = UpdateStatus(state="failed", step="fetch", message="fetch: fatal: x", reason="fetch_failed", params={"detail": "fatal: x"})
    write_status(tmp_path, status)
    assert read_status(tmp_path) == status


def test_a_file_without_reason_or_with_an_unknown_one_reads_as_before(tmp_path):
    status_path(tmp_path).write_text(
        '{"state": "failed", "step": "fetch", "message": "fetch: boom", "reason": "from_the_future", "params": ["x"]}',
        encoding="utf-8",
    )
    read = read_status(tmp_path)
    assert (read.state, read.message, read.reason, read.params) == ("failed", "fetch: boom", "", {})
    status_path(tmp_path).write_text('{"state": "failed", "message": "fetch: boom"}', encoding="utf-8")
    assert read_status(tmp_path).reason == ""


def test_acknowledged_survives_the_file_and_defaults_to_false(tmp_path):  # p503
    status = UpdateStatus(state="failed", step="fetch", acknowledged=True)
    write_status(tmp_path, status)
    assert read_status(tmp_path).acknowledged is True
    (tmp_path / "update-status.json").write_text('{"state": "succeeded"}', encoding="utf-8")  # a file of 0.4.5
    assert read_status(tmp_path).acknowledged is False


@pytest.mark.parametrize("reason", ["package_missing", "checksum_mismatch", "package_unusable"])
def test_v049_the_reasons_of_the_package_survive_the_status_file(tmp_path, reason):
    write_status(tmp_path, UpdateStatus(state="failed", step="fetch", started_at=NOW, reason=reason, params={"detail": "d"}))
    assert read_status(tmp_path).reason == reason
