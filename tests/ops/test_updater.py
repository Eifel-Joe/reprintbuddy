import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops.backup import backup_dir_for
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.service import RESTART_WAIT_CMD
from reprintbuddy.ops.status import UpdateStatus, read_status, write_status
from reprintbuddy.ops.updater import http_health_waiter, run_update, venv_python

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
REF = "a" * 40


class Env:
    """A database with queue_cap 7, a fake install dir and a runner that behaves like a good update."""

    def __init__(self, tmp_path, fake_runner):
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.db = self.data / "u.db"
        engine = make_engine(self.db)
        upgrade(engine)
        self.maker = make_sessionmaker(engine)
        with self.maker() as session:
            st.set_setting(session, "queue_cap", 7)
        self.opt = tmp_path / "opt"
        (self.opt / ".git").mkdir(parents=True)
        self.runner = fake_runner
        self.d = str(self.opt)
        self.py = str(venv_python(self.opt))
        git = ("git", "-C", self.d)
        fake_runner.on(git + ("describe",), CommandResult(0, "v0.1.0\n"))
        fake_runner.on(git + ("rev-parse",), CommandResult(0, REF + "\n"))
        fake_runner.on(git + ("tag", "--list"), CommandResult(0, "v0.1.0\nv0.2.0\n"))
        fake_runner.on((self.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 0.2.0\n"))
        self.health_answers = [True]

    def wait_healthy(self):
        answers = self.health_answers
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def update(self, **kwargs):
        return run_update(
            self.db,
            install_dir=self.opt,
            runner=self.runner,
            wait_healthy=self.wait_healthy,
            now=lambda: NOW,
            **kwargs,
        )

    def queue_cap(self):
        con = sqlite3.connect(self.db)
        try:
            return int(con.execute("SELECT value FROM settings WHERE key='queue_cap'").fetchone()[0])
        finally:
            con.close()

    def called(self, *prefix):
        return [c for c in self.runner.calls if c[: len(prefix)] == prefix]


@pytest.fixture
def update_env(tmp_path, fake_runner):
    return Env(tmp_path, fake_runner)


def test_venv_python(tmp_path):
    assert venv_python(tmp_path) == tmp_path / ".venv" / "bin" / "python"


def test_happy_path_commands(update_env):
    e = update_env
    e.update()
    git = ("git", "-C", e.d)
    assert e.runner.calls == [
        git + ("describe", "--tags", "--exact-match", "HEAD"),
        git + ("rev-parse", "HEAD"),
        git + ("fetch", "--tags", "origin"),
        git + ("tag", "--list", "v*"),
        git + ("checkout", "--quiet", "--detach", "v0.2.0"),
        (e.py, "-m", "pip", "install", "--no-cache-dir", "--upgrade", e.d),
        (e.py, "-m", "reprintbuddy", "--version"),
        (e.py, "-m", "reprintbuddy", "migrate", "--db", str(e.db)),
        RESTART_WAIT_CMD,
    ]


def test_happy_path_status(update_env):
    e = update_env
    result = e.update()
    stored = read_status(e.data)
    assert result == stored
    assert stored.state == "succeeded" and stored.step == "done"
    assert (stored.from_tag, stored.to_tag) == ("v0.1.0", "v0.2.0")
    assert stored.finished_at is not None
    from pathlib import Path

    assert stored.backup and Path(stored.backup).exists()


def test_backup_before_fetch(update_env):
    e = update_env
    seen = {}

    def at_fetch(call):
        seen["files"] = list(backup_dir_for(e.db).iterdir())
        return CommandResult(0)

    e.runner.on(("git", "-C", e.d, "fetch"), at_fetch)
    e.update()
    assert len(seen["files"]) == 1


def test_status_written_per_step(update_env):
    e = update_env
    seen = {}

    def at_pip(call):
        status = read_status(e.data)
        seen["state"], seen["step"] = status.state, status.step
        return CommandResult(0)

    e.runner.on((e.py, "-m", "pip"), at_pip)
    e.update()
    assert seen == {"state": "running", "step": "install"}


def test_no_newer_release(update_env):
    e = update_env
    e.runner.on(("git", "-C", e.d, "tag", "--list"), CommandResult(0, "v0.1.0\n"))
    result = e.update()
    assert result.state == "failed"
    assert "no newer release" in result.message
    assert not e.called("git", "-C", e.d, "checkout")
    assert RESTART_WAIT_CMD not in e.runner.calls


@pytest.mark.parametrize("target", ["v9.9.9", "main"])
def test_unknown_target(update_env, target):
    e = update_env
    result = e.update(target=target)
    assert result.state == "failed"
    assert f"unknown target {target}" in result.message
    assert not e.called("git", "-C", e.d, "checkout")


def test_explicit_target_must_be_newer(update_env):
    result = update_env.update(target="v0.1.0")
    assert result.state == "failed" and "no newer release" in result.message


def test_fetch_fails_nothing_changed(update_env):
    e = update_env
    e.runner.on(("git", "-C", e.d, "fetch"), CommandResult(1, "", "fatal: no network\nmore"))
    result = e.update()
    assert result.state == "failed" and result.step == "fetch"
    assert result.message == "fetch: fatal: no network"
    assert not e.called("git", "-C", e.d, "checkout")
    assert RESTART_WAIT_CMD not in e.runner.calls
    assert e.queue_cap() == 7


def test_rev_parse_failure_is_a_failure_before_anything_changed(update_env):
    e = update_env
    e.runner.on(("git", "-C", e.d, "rev-parse"), CommandResult(128, "", "fatal: not a git repository"))
    result = e.update()
    assert result.state == "failed"
    assert not e.called("git", "-C", e.d, "checkout")


def test_missing_database_is_a_failure_not_a_traceback(update_env):
    e = update_env
    for suffix in ("", "-wal", "-shm"):
        (e.data / ("u.db" + suffix)).unlink(missing_ok=True)
    result = e.update()
    assert result.state == "failed" and result.step == "backup"
    assert not e.db.exists()


def test_pip_fails_rolls_back(update_env):
    e = update_env
    e.runner.on((e.py, "-m", "pip"), CommandResult(1, "", "ERROR: no network"), CommandResult(0))
    result = e.update()
    assert ("git", "-C", e.d, "checkout", "--quiet", "--detach", REF) in e.runner.calls
    assert len(e.called(e.py, "-m", "pip")) == 2
    assert RESTART_WAIT_CMD in e.runner.calls
    assert result.state == "rolled_back"
    assert result.message.startswith("install:")


def test_version_mismatch_rolls_back(update_env):
    e = update_env
    e.runner.on((e.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 0.1.9\n"))
    result = e.update()
    assert result.state == "rolled_back"
    assert result.message.startswith("verify:")


def test_migrate_fails_restores_db(update_env):
    e = update_env

    def broken_migration(call):
        with e.maker() as session:
            st.set_setting(session, "queue_cap", 99)
        return CommandResult(1, "", "migration failed")

    e.runner.on((e.py, "-m", "reprintbuddy", "migrate"), broken_migration)
    result = e.update()
    assert e.queue_cap() == 7
    assert result.state == "rolled_back" and result.message.startswith("migrate:")


def test_unhealthy_after_restart_rolls_back(update_env):
    e = update_env
    e.health_answers = [False, True]
    result = e.update()
    assert result.state == "rolled_back"
    assert result.message.startswith("health:")
    assert e.runner.calls.count(RESTART_WAIT_CMD) == 2


def test_restart_command_failure_rolls_back(update_env):
    e = update_env
    e.runner.on(RESTART_WAIT_CMD, CommandResult(1, "", "sudo: a password is required"), CommandResult(0))
    result = e.update()
    assert result.state == "rolled_back" and result.message.startswith("restart:")


def test_rollback_fails(update_env):
    e = update_env
    e.health_answers = [False]
    result = e.update()
    assert result.state == "rollback_failed"
    assert result.backup and result.backup in result.message
    assert " – backup: " in result.message


def test_rollback_checkout_failure_is_reported(update_env):
    e = update_env
    e.runner.on((e.py, "-m", "pip"), CommandResult(1, "", "no"))
    e.runner.on(("git", "-C", e.d, "checkout"), CommandResult(0), CommandResult(1, "", "cannot checkout"))
    result = e.update()
    assert result.state == "rollback_failed"


def test_refuses_when_running(update_env):
    e = update_env
    running = UpdateStatus(state="running", step="install", started_at=NOW - timedelta(minutes=1))
    write_status(e.data, running)
    result = e.update()
    assert result == running
    assert e.runner.calls == []


def test_stale_running_state_does_not_block(update_env):
    e = update_env
    write_status(e.data, UpdateStatus(state="running", started_at=NOW - timedelta(minutes=31)))
    assert e.update().state == "succeeded"


def test_takes_over_queued(update_env):
    e = update_env
    write_status(e.data, UpdateStatus(state="queued", step="start", started_at=NOW - timedelta(minutes=1)))
    assert e.update().state == "succeeded"


def test_prunes_backups(update_env):
    e = update_env
    folder = backup_dir_for(e.db)
    folder.mkdir()
    for i in range(6):
        (folder / f"reprintbuddy-v0.0.{i}-2026010{i + 1}T000000Z.db").write_bytes(b"x")
    e.update()
    assert len(list(folder.iterdir())) == 5


def test_dev_build_without_tag_labels_backup_with_the_commit(update_env):
    e = update_env
    e.runner.on(("git", "-C", e.d, "describe"), CommandResult(128, "", "fatal: no tag"))
    result = e.update()
    assert result.state == "succeeded"
    assert result.from_tag is None
    assert "reprintbuddy-" + REF[:12] + "-" in result.backup


def test_health_waiter(respx_mock, tmp_path):
    engine = make_engine(tmp_path / "h.db")
    upgrade(engine)
    route = respx_mock.get("http://127.0.0.1:8080/healthz").respond(200, json={"status": "ok"})
    assert http_health_waiter(tmp_path / "h.db")() is True
    assert route.called


def test_health_waiter_gives_up(respx_mock, tmp_path):
    engine = make_engine(tmp_path / "h.db")
    upgrade(engine)
    respx_mock.get("http://127.0.0.1:8080/healthz").respond(503)
    assert http_health_waiter(tmp_path / "h.db", timeout_s=0.3, interval_s=0.1)() is False


def test_health_waiter_follows_the_saved_address(respx_mock, tmp_path):
    from conftest import make_tls_pair

    engine = make_engine(tmp_path / "h.db")
    upgrade(engine)
    cert, key = make_tls_pair()
    with make_sessionmaker(engine)() as session:
        st.set_setting(session, "port", 8443)
        st.set_setting(session, "host", "127.0.0.1")
        st.set_setting(session, "tls_cert", cert)
        st.set_setting(session, "tls_key", key)
    route = respx_mock.get("https://127.0.0.1:8443/healthz").respond(200, json={"status": "ok"})
    assert http_health_waiter(tmp_path / "h.db")() is True
    assert route.called


def test_health_waiter_survives_connection_errors(respx_mock, tmp_path):
    engine = make_engine(tmp_path / "h.db")
    upgrade(engine)
    respx_mock.get("http://127.0.0.1:8080/healthz").mock(side_effect=httpx.ConnectError("down"))
    assert http_health_waiter(tmp_path / "h.db", timeout_s=0.3, interval_s=0.1)() is False
