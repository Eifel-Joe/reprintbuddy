import sqlite3
from pathlib import Path
from datetime import UTC, datetime, timedelta

import pytest

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops.backup import backup_dir_for
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.service import RESTART_WAIT_CMD, START_CMD, STOP_CMD
from reprintbuddy.ops.status import UpdateStatus, read_status, write_status
from reprintbuddy.ops.updater import run_update, venv_python

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
        git + ("fetch", "--prune", "--prune-tags", "--force", "--tags", "origin"),
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
    assert (result.reason, result.params) == ("no_newer", {})  # p176
    assert not e.called("git", "-C", e.d, "checkout")
    assert RESTART_WAIT_CMD not in e.runner.calls


@pytest.mark.parametrize("target", ["v9.9.9", "main"])
def test_unknown_target(update_env, target):
    e = update_env
    result = e.update(target=target)
    assert result.state == "failed"
    assert f"unknown target {target}" in result.message
    assert (result.reason, result.params) == ("unknown_target", {"detail": target})  # p176
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
    assert (result.reason, result.params) == ("fetch_failed", {"detail": "fatal: no network"})  # p176
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
    assert STOP_CMD in e.runner.calls and START_CMD in e.runner.calls
    assert result.state == "rolled_back"
    assert result.message.startswith("install:")
    assert (result.reason, result.params) == ("install_failed", {"detail": "ERROR: no network"})  # p176


def test_pip_offline_in_the_rollback_too_is_still_a_rollback(update_env):
    """No PyPI: the reinstall of the old version fails as well, but the old code is
    checked out, still installed, and answers. That is a working rollback."""
    e = update_env
    e.runner.on((e.py, "-m", "pip"), CommandResult(1, "", "ERROR: no network"))
    e.runner.on((e.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 0.1.0\n"))
    result = e.update()
    assert result.state == "rolled_back", result.message
    assert result.message.startswith("install:")


def test_rollback_with_an_unverifiable_old_version_is_reported_as_failed(update_env):
    e = update_env
    e.runner.on((e.py, "-m", "pip"), CommandResult(1, "", "ERROR: no network"))
    e.runner.on((e.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 0.2.0\n"))
    assert e.update().state == "rollback_failed"


def test_version_mismatch_rolls_back(update_env):
    e = update_env
    e.runner.on((e.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 0.1.9\n"))
    result = e.update()
    assert result.state == "rolled_back"
    assert result.message.startswith("verify:")
    assert result.reason == "verify_failed"  # p176
    assert result.params == {"expected": "reprintbuddy 0.2.0", "seen": "reprintbuddy 0.1.9"}


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
    assert (result.reason, result.params) == ("migrate_failed", {"detail": "migration failed"})  # p176


def test_unhealthy_after_restart_rolls_back(update_env):
    e = update_env
    e.health_answers = [False, True]
    result = e.update()
    assert result.state == "rolled_back"
    assert result.message.startswith("health:")
    assert (result.reason, result.params) == ("health_failed", {})  # p176
    assert e.runner.calls.count(RESTART_WAIT_CMD) == 1  # the update's own restart
    assert e.runner.calls[-1] == START_CMD or START_CMD in e.runner.calls


def test_restart_command_failure_rolls_back(update_env):
    e = update_env
    e.runner.on(RESTART_WAIT_CMD, CommandResult(1, "", "sudo: a password is required"))
    result = e.update()
    assert result.state == "rolled_back" and result.message.startswith("restart:")
    assert (result.reason, result.params) == ("restart_failed", {"detail": "sudo: a password is required"})  # p176


def test_rollback_fails(update_env):
    e = update_env
    e.health_answers = [False]
    result = e.update()
    assert result.state == "rollback_failed"
    assert result.backup and result.backup in result.message
    assert " – backup: " in result.message
    assert result.reason == "health_failed"  # p176: the cause stays named when the rollback fails too


def test_the_database_is_restored_while_the_service_is_stopped(update_env):
    """Restoring under a running service leaves it with connections to a replaced database."""
    e = update_env
    seen = {}

    def broken_migration(call):
        with e.maker() as session:
            st.set_setting(session, "queue_cap", 99)
        return CommandResult(1, "", "migration failed")

    e.runner.on((e.py, "-m", "reprintbuddy", "migrate"), broken_migration)
    e.runner.on(STOP_CMD, lambda call: seen.setdefault("at_stop", e.queue_cap()) and CommandResult(0))
    e.runner.on(START_CMD, lambda call: seen.setdefault("at_start", e.queue_cap()) and CommandResult(0))
    assert e.update().state == "rolled_back"
    assert seen == {"at_stop": 99, "at_start": 7}
    calls = e.runner.calls
    assert calls.index(STOP_CMD) < calls.index(START_CMD)


def test_a_stop_that_fails_falls_back_to_a_restart(update_env):
    """If the service cannot be stopped it may still run the new code: restart it."""
    e = update_env
    e.runner.on((e.py, "-m", "pip"), CommandResult(1, "", "ERROR: no network"))
    e.runner.on(STOP_CMD, CommandResult(1, "", "sudo: not allowed"))
    e.runner.on((e.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 0.1.0\n"))
    result = e.update()
    assert result.state == "rolled_back"
    assert START_CMD not in e.runner.calls
    assert RESTART_WAIT_CMD in e.runner.calls


def test_a_start_that_fails_in_the_rollback_is_a_failed_rollback(update_env):
    e = update_env
    e.runner.on((e.py, "-m", "pip"), CommandResult(1, "", "no"), CommandResult(0))
    e.runner.on(START_CMD, CommandResult(1, "", "Job failed"))
    assert e.update().state == "rollback_failed"


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


def make_venv(e, content="old"):
    folder = e.opt / ".venv" / "lib"
    folder.mkdir(parents=True)
    (folder / "marker.txt").write_text(content, encoding="utf-8")
    return folder / "marker.txt"


def pip_that_changes_the_environment(e, marker, *, ok):
    def pip(call):
        marker.write_text("new", encoding="utf-8")
        return CommandResult(0 if ok else 1, "", "" if ok else "ERROR: half way")

    e.runner.on((e.py, "-m", "pip"), pip)


def test_the_rollback_restores_the_python_environment(update_env):
    """pip that fails half way may leave the venv neither old nor new."""
    e = update_env
    marker = make_venv(e)
    pip_that_changes_the_environment(e, marker, ok=False)
    result = e.update()
    assert result.state == "rolled_back", result.message
    assert marker.read_text(encoding="utf-8") == "old"
    assert not (e.opt / ".venv.rollback").exists()
    assert len(e.called(e.py, "-m", "pip")) == 1  # the snapshot came back, no reinstall needed


def test_a_version_mismatch_restores_the_python_environment_too(update_env):
    e = update_env
    marker = make_venv(e)
    pip_that_changes_the_environment(e, marker, ok=True)
    e.runner.on((e.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 0.1.9\n"))
    assert e.update().state == "rolled_back"
    assert marker.read_text(encoding="utf-8") == "old"


def test_the_snapshot_is_removed_after_a_successful_update(update_env):
    e = update_env
    marker = make_venv(e)
    pip_that_changes_the_environment(e, marker, ok=True)
    assert e.update().state == "succeeded"
    assert marker.read_text(encoding="utf-8") == "new"
    assert not (e.opt / ".venv.rollback").exists()


def test_a_leftover_snapshot_of_an_earlier_run_is_replaced(update_env):
    e = update_env
    marker = make_venv(e)
    (e.opt / ".venv.rollback").mkdir()
    (e.opt / ".venv.rollback" / "stale.txt").write_text("stale", encoding="utf-8")
    seen = {}

    def pip(call):
        seen["files"] = sorted(p.name for p in (e.opt / ".venv.rollback").rglob("*") if p.is_file())
        return CommandResult(0)

    e.runner.on((e.py, "-m", "pip"), pip)
    e.update()
    assert seen["files"] == ["marker.txt"]


def test_a_stale_snapshot_is_never_restored_over_an_untouched_environment(update_env):
    """Checkout fails before this run took its own snapshot; an old leftover must not come back."""
    e = update_env
    marker = make_venv(e, "current")
    stale = e.opt / ".venv.rollback" / "lib"
    stale.mkdir(parents=True)
    (stale / "marker.txt").write_text("stale", encoding="utf-8")
    # the first checkout (forward) fails; the rollback's checkout then succeeds
    e.runner.on(("git", "-C", e.d, "checkout"), CommandResult(1, "", "fatal: bad"), CommandResult(0))
    result = e.update()
    assert result.state == "rolled_back" and result.message.startswith("checkout:")
    assert marker.read_text(encoding="utf-8") == "current"


def test_a_failed_snapshot_stops_before_pip_touches_anything(update_env, monkeypatch):
    e = update_env
    make_venv(e)

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr("reprintbuddy.ops.updater.shutil.copytree", boom)
    result = e.update()
    assert result.state == "rolled_back"
    assert result.message.startswith("install: cannot back up the Python environment")
    assert (result.reason, result.params) == ("snapshot_failed", {"detail": "no space left on device"})  # p176
    forward_pip = [c for c in e.runner.calls if c[:3] == (e.py, "-m", "pip")]
    assert len(forward_pip) == 1  # only the rollback's own reinstall; the update never ran pip


def queue(e, to_tag):
    write_status(
        e.data,
        UpdateStatus(state="queued", step="start", from_tag="v0.1.0", to_tag=to_tag, started_at=NOW - timedelta(minutes=1)),
    )


def test_update_goes_to_the_version_the_page_offered(update_env):
    """A newer tag published between 'Check for updates' and the click is not installed."""
    e = update_env
    e.runner.on(("git", "-C", e.d, "tag", "--list"), CommandResult(0, "v0.1.0\nv0.2.0\nv0.3.0\n"))
    e.runner.on((e.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 0.2.0\n"))
    queue(e, "v0.2.0")
    result = e.update()
    assert result.state == "succeeded" and result.to_tag == "v0.2.0"
    assert ("git", "-C", e.d, "checkout", "--quiet", "--detach", "v0.2.0") in e.runner.calls


def test_an_explicit_target_wins_over_the_queued_one(update_env):
    e = update_env
    e.runner.on(("git", "-C", e.d, "tag", "--list"), CommandResult(0, "v0.1.0\nv0.2.0\nv0.3.0\n"))
    e.runner.on((e.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 0.3.0\n"))
    queue(e, "v0.2.0")
    assert e.update(target="v0.3.0").to_tag == "v0.3.0"


@pytest.mark.parametrize("offered", ["v0.2.0", "latest", "v01.2.0"])
def test_a_queued_version_that_is_gone_or_invalid_is_not_replaced_by_another(update_env, offered):
    """The tag was deleted on GitHub meanwhile: do not silently install something else."""
    e = update_env
    e.runner.on(("git", "-C", e.d, "tag", "--list"), CommandResult(0, "v0.1.0\nv0.3.0\n"))
    queue(e, offered)
    result = e.update()
    assert result.state == "failed" and f"unknown target {offered}" in result.message
    assert not e.called("git", "-C", e.d, "checkout")


def test_reason_is_the_line_that_says_what_went_wrong(update_env):
    """git prints 'From https://...' first, pip a retry warning, Python a Traceback header."""
    e = update_env
    e.runner.on(
        ("git", "-C", e.d, "fetch"),
        CommandResult(128, "", "From https://example.invalid/r\n * [new tag] v0.3.0\nfatal: unable to access 'x': Could not resolve host"),
    )
    assert e.update().message == "fetch: fatal: unable to access 'x': Could not resolve host"


def test_reason_of_a_pip_failure_skips_the_retry_warnings(update_env):
    e = update_env
    e.runner.on(
        (e.py, "-m", "pip"),
        CommandResult(1, "", "WARNING: Retrying (Retry(total=4)) after connection broken\nERROR: Could not find a version that satisfies the requirement x\nWARNING: later noise"),
    )
    assert e.update().message.startswith("install: ERROR: Could not find a version")


@pytest.mark.parametrize("found", ["3.12.3", "3.11.2"])
def test_an_environment_with_an_older_python_than_the_new_version_needs_rolls_back_and_says_so(update_env, found):
    """v0.3.9 p101, floor 3.13 since v0.3.12 p163: pip refuses a release whose Python floor is above the
    environment's Python; the old version goes on and the page names the reason in plain words, not a half
    update. This is the updater of v0.3.9 and later (the one of an installed v0.3.8 on Debian 12 is the old
    code and shows pip's raw line: the rollback below is the same either way)."""
    e = update_env
    pip_says = (
        "Processing /opt/reprintbuddy\n"
        "  Installing build dependencies: started\n"
        f"ERROR: Package 'reprintbuddy' requires a different Python: {found} not in '>=3.13'"
    )
    e.runner.on((e.py, "-m", "pip"), CommandResult(1, "", pip_says), CommandResult(0))
    result = e.update()
    assert result.state == "rolled_back", result.message
    assert ("git", "-C", e.d, "checkout", "--quiet", "--detach", REF) in e.runner.calls  # back on the old code
    assert STOP_CMD in e.runner.calls and START_CMD in e.runner.calls  # and the service was started again
    assert result.message.startswith("install:")
    for word in ("Python 3.13", f"Python {found}", "install command"):
        assert word in result.message, result.message
    assert (result.reason, result.params) == ("python_too_old", {"needed": "3.13", "found": found})  # p176
    assert "ERROR" not in result.message  # pip's line is not shown raw


def test_a_different_python_error_of_another_kind_keeps_pips_own_line(update_env):
    e = update_env
    e.runner.on((e.py, "-m", "pip"), CommandResult(1, "", "ERROR: Could not find a version that satisfies the requirement x"), CommandResult(0))
    assert e.update().message.startswith("install: ERROR: Could not find a version")


def test_reason_of_a_failed_migration_has_no_sql_and_no_parameters(update_env):
    e = update_env
    trace = (
        "Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n"
        "sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: widgets\n"
        "[SQL: INSERT INTO settings (key, value) VALUES (?, ?)]\n"
        "[parameters: ('bambuddy_api_key', 'bb_SECRETKEY')]\n"
        "(Background on this error at: https://sqlalche.me/e/20/e3q8)"
    )
    e.runner.on((e.py, "-m", "reprintbuddy", "migrate"), CommandResult(1, "", trace))
    message = e.update().message
    assert message.startswith("migrate: sqlalchemy.exc.OperationalError")
    assert "SECRETKEY" not in message and "[SQL" not in message and "parameters" not in message


def test_reason_cuts_an_inline_sql_part(update_env):
    e = update_env
    e.runner.on(
        (e.py, "-m", "reprintbuddy", "migrate"),
        CommandResult(1, "", "OperationalError: no such table: x [SQL: SELECT 'bb_SECRETKEY']"),
    )
    message = e.update().message
    assert "SECRETKEY" not in message and message.startswith("migrate: OperationalError: no such table: x")










def test_sigterm_handler_writes_the_final_state_and_exits(update_env):
    from reprintbuddy.ops.updater import interrupt_handler

    e = update_env
    write_status(e.data, UpdateStatus(state="running", step="install", started_at=NOW, backup="/b/x.db"))
    handler = interrupt_handler(e.db, now=lambda: NOW + timedelta(minutes=30))
    with pytest.raises(SystemExit) as stop:
        handler(15, None)
    assert stop.value.code == 143
    assert read_status(e.data).state == "rollback_failed"


def test_the_backup_named_by_a_failed_rollback_is_not_pruned(update_env):
    """The message tells the user to restore exactly this file; cleaning up must not delete it."""
    e = update_env
    folder = backup_dir_for(e.db)
    folder.mkdir()
    for i in range(1, 6):  # five newer backups: this run's backup is the oldest
        (folder / f"reprintbuddy-v9.0.{i}-2027010{i}T000000Z.db").write_bytes(b"x")
    e.health_answers = [False]
    result = e.update()
    assert result.state == "rollback_failed"
    assert Path(result.backup).exists()


def test_a_backup_is_pruned_normally_after_a_success(update_env):
    e = update_env
    folder = backup_dir_for(e.db)
    folder.mkdir()
    for i in range(1, 6):
        (folder / f"reprintbuddy-v9.0.{i}-2027010{i}T000000Z.db").write_bytes(b"x")
    result = e.update()
    assert result.state == "succeeded" and not Path(result.backup).exists()


def test_a_status_file_that_cannot_be_written_does_not_stop_the_rollback(update_env, monkeypatch):
    from reprintbuddy.ops import updater as module

    e = update_env
    real = module.write_status

    def flaky(data_dir, status):
        if status.step in ("rollback", "health") or status.state == "rolled_back":
            raise OSError("no space left on device")
        real(data_dir, status)

    monkeypatch.setattr(module, "write_status", flaky)
    e.health_answers = [False, True]
    result = e.update()
    assert result.state == "rolled_back"
    assert START_CMD in e.runner.calls or RESTART_WAIT_CMD in e.runner.calls
    assert e.queue_cap() == 7


def test_sigterm_handler_exits_even_if_the_status_cannot_be_written(update_env, monkeypatch):
    from reprintbuddy.ops import status as status_module
    from reprintbuddy.ops.updater import interrupt_handler

    e = update_env
    write_status(e.data, UpdateStatus(state="running", step="install", started_at=NOW))

    def broken(data_dir, status):
        raise OSError("read-only file system")

    monkeypatch.setattr(status_module, "write_status", broken)
    with pytest.raises(SystemExit) as stop:
        interrupt_handler(e.db, now=lambda: NOW)(15, None)
    assert stop.value.code == 143


def test_a_status_file_that_cannot_be_written_does_not_stop_the_update(update_env, monkeypatch):
    from reprintbuddy.ops import updater as module

    e = update_env

    def broken(data_dir, status):
        raise OSError("read-only file system")

    monkeypatch.setattr(module, "write_status", broken)
    assert e.update().state == "succeeded"



def test_an_update_refuses_while_a_restore_holds_the_lock(update_env, capsys):
    """p91: by hand on the command line during a restore from the page (or the other way round)."""
    from reprintbuddy.ops import oplock

    write_status(update_env.data, UpdateStatus(state="succeeded", step="done"))
    held = oplock.acquire(update_env.data, "restore")
    try:
        result = update_env.update()
    finally:
        held.release()
    assert result.state == "running"  # review C 3: the command exits 3, not 0 - nothing was done
    assert update_env.runner.calls == []
    assert read_status(update_env.data).state == "succeeded"
    assert "a restore is running" in capsys.readouterr().out
    assert not backup_dir_for(update_env.db).exists()  # not even the backup was made


def test_an_update_holds_the_lock_while_it_runs_and_lets_it_go(update_env):
    from reprintbuddy.ops import oplock

    seen = []
    real = update_env.wait_healthy

    def healthy():
        seen.append(oplock.holder(update_env.data))
        return real()

    update_env.wait_healthy = healthy
    assert update_env.update().state == "succeeded"
    assert seen and set(seen) == {"update"}
    assert oplock.holder(update_env.data) is None


def test_an_update_whose_lock_file_cannot_be_opened_is_refused_cleanly(update_env, monkeypatch, capsys):
    """Review C 5: no traceback; nothing done."""
    from reprintbuddy.ops import updater

    def denied(*args, **kwargs):
        raise PermissionError("maintenance.lock")

    monkeypatch.setattr(updater, "acquire", denied)
    result = update_env.update()
    assert result.state == "running" and update_env.runner.calls == []
    assert "lock" in capsys.readouterr().out
