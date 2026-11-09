"""Backup and update in the activity log (Spec 10.7, group 4): full backup (encrypted or not), export,
restore refused with a fixed reason, cancelled or done, restart, update started, a new version once."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from sqlalchemy import select

from reprintbuddy.models import ActivityEntry
from reprintbuddy.ops import fullbackup as fb

from conftest import csrf_of
from reprintbuddy.ops.runner import CommandResult

# The helpers of the tests of the backup page, as they are (that module imports only conftest).
_HERE = pathlib.Path(__file__).resolve().parents[1] / "web"
_spec = importlib.util.spec_from_file_location("_activity_backup_helpers", _HERE / "test_ops_full_backup.py")
backup_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_tests)

REMOTE_TWO = CommandResult(0, "abc\trefs/tags/v0.1.0\ndef\trefs/tags/v0.2.0\n")


def git_env(env, *, describe="v0.1.0\n"):
    """The fake install dir as a git installation with a newer tag (as in test_ops_update)."""
    (env.install_dir / ".git").mkdir(parents=True, exist_ok=True)
    d = str(env.install_dir)
    env.runner.on(("git", "-C", d, "describe"), CommandResult(0, describe))
    env.runner.on(("git", "-C", d, "ls-remote"), REMOTE_TWO)


def post(env, path):
    return env.client.post(path, data={"csrf_token": csrf_of(env.client, "/settings")})


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(fb, "KDF", backup_tests.FAST)


def ops_lines(env) -> list[tuple[str, str, dict | None]]:
    env.client.app.state.activity.drain()
    with env.client.app.state.sessionmaker() as s:
        rows = s.scalars(select(ActivityEntry).where(ActivityEntry.area == "ops").order_by(ActivityEntry.id))
        return [(row.kind, row.actor, row.details) for row in rows]


def test_a_full_backup_says_whether_it_is_encrypted(ops_env):
    backup_tests.made_backup(ops_env)
    backup_tests.made_backup(ops_env, backup_tests.BACKUP_PASSWORD)
    assert ops_lines(ops_env) == [
        ("backup_downloaded", "user", {"encrypted": False}),
        ("backup_downloaded", "user", {"encrypted": True}),
    ]
    assert backup_tests.BACKUP_PASSWORD not in repr(ops_lines(ops_env))


def test_the_export(ops_env):
    response = ops_env.client.get("/settings/export")
    assert response.status_code == 200
    assert ops_lines(ops_env) == [("export_downloaded", "user", None)]


def test_a_foreign_file_is_refused_with_its_reason(ops_env):
    backup_tests.upload(ops_env, b"not a backup at all")
    assert ops_lines(ops_env) == [("restore_rejected", "user", {"reason": "foreign"})]


def test_a_cancelled_restore(ops_env):
    content = backup_tests.made_backup(ops_env)
    backup_tests.upload(ops_env, content)
    backup_tests.post(ops_env, "/settings/restore/cancel")
    assert [kind for kind, _actor, _details in ops_lines(ops_env)][-1] == "restore_cancelled"


def test_a_done_restore_is_logged_by_the_next_start_into_the_restored_database(ops_env):
    content = backup_tests.made_backup(ops_env)
    backup_tests.upload(ops_env, content)
    response = backup_tests.confirm(ops_env)
    assert response.status_code == 200, response.text
    marker = ops_env.db_path.parent / "activity-restored.json"
    assert marker.is_file()
    from conftest import make_client

    restarted = make_client(ops_env.db_path)
    with restarted:
        restarted.app.state.activity.drain()
    with restarted.app.state.sessionmaker() as s:
        kinds = [row.kind for row in s.scalars(select(ActivityEntry).order_by(ActivityEntry.id))]
        done = s.scalars(select(ActivityEntry).where(ActivityEntry.kind == "restore_done")).one()
    assert kinds[-2:] == ["restore_done", "started"]
    assert set(done.details) == {"created_at", "version"}
    assert not marker.exists()


def test_a_restart_that_was_asked_for(ops_env):
    response = backup_tests.post(ops_env, "/settings/restart")
    assert response.status_code == 200
    assert ops_lines(ops_env) == [("restart_requested", "user", None)]


def test_the_new_version_once_and_the_start_of_an_update_with_its_result(ops_env):
    """The start and the result come from the state file of the update, at the next look after it
    ended: the old process writes nothing while the update runs, and a rollback would take a line of
    that time with it (Spec 11.3a; review v0.3.13 C, finding 3)."""
    from datetime import UTC, datetime, timedelta

    from reprintbuddy.ops.status import UpdateStatus, write_status

    git_env(ops_env)
    post(ops_env, "/settings/update/check")
    post(ops_env, "/settings/update/check")  # found again: no second line
    assert post(ops_env, "/settings/update").status_code == 303
    app = ops_env.client.app
    # While the update runs, nothing is written: also after its first seconds.
    import asyncio

    write_status(ops_env.db_path.parent, UpdateStatus(
        state="running", step="install", from_tag="v0.1.0", to_tag="v0.2.0",
        started_at=datetime.now(UTC) - timedelta(minutes=3),
    ))
    app.state.activity.record("login", "user")
    assert asyncio.run(app.state.activity.drain_counted()) is False
    started = datetime.now(UTC) - timedelta(minutes=3)
    write_status(ops_env.db_path.parent, UpdateStatus(
        state="succeeded", step="done", from_tag="v0.1.0", to_tag="v0.2.0",
        started_at=started, finished_at=started + timedelta(minutes=2), message="pip: text",
    ))
    app.state.activity.check_update()
    assert asyncio.run(app.state.activity.drain_counted()) is True
    found = [line for line in ops_lines(ops_env) if line[0] != "login"]
    assert found == [
        ("version_found", "user", {"version": "v0.2.0"}),
        ("update_started", "user", {"from": "v0.1.0", "to": "v0.2.0"}),
        ("update_result", "system", {"from": "v0.1.0", "to": "v0.2.0", "result": "done"}),
    ]
