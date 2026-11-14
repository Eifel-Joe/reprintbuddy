"""The update helper goes the way back from a pre-release to the stable version (Spec 11.3a "Rückweg").

Order as the spec says it: lock, backup of the current state, package of the stable version loaded and
checked; then the service stops, the database (a copy of it as it was is kept, `.return/before.db`), the
package, the start, the check that the stable version answers. The live database is changed once, in one
SQLite transaction (`swap`), before pip. A failure leaves the pre-release running with exactly the data from
before the way back; a request that changed or grew old changes nothing. Real Alembic, real SQLite, a real
package; systemd, pip and GitHub are stood in for.
"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from beta_steps import beta_scripts, revision_of
from github_support import FakeGitHub
from migration_rules import snapshot_of
from package_support import build_archive

from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops import updater
from reprintbuddy.ops.backup import backup_database, backup_dir_for
from reprintbuddy.ops.installation import read_installation, write_marker
from reprintbuddy.ops.migrationsteps import current_revision
from reprintbuddy.ops.releases import package_name
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.service import RESTART_WAIT_CMD, START_CMD, STOP_CMD
from reprintbuddy.ops.status import UpdateStatus, read_status, write_status
from reprintbuddy.ops.updater import RETURN_DIR, run_update, venv_python

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
BACKUP_AT = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
BETA, STABLE = "v1.1.0-beta.1", "v1.0.0"


def sql(path, statement, *args):
    con = sqlite3.connect(path)
    try:
        rows = con.execute(statement, args).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def add_journal(path):
    sql(path, "INSERT INTO journal (at, booked_at, actor, kind, qty) VALUES ('2026-11-12 10:00:00', '2026-11-12 10:00:00', 'user', 'stock_in', 1)")


def channel(path):
    rows = sql(path, "SELECT value FROM settings WHERE key = 'update_channel'")
    return json.loads(rows[0][0]) if rows else None


def content(path):
    """Everything that counts in a database: revision, schema and rows."""
    shot = snapshot_of(path)
    return current_revision(path), {name: (table.columns, table.sql) for name, table in shot.tables.items()}, shot.rows


class ReturnEnv:
    """A pre-release v1.1.0-beta.1 with `steps` after the stable head, its data, the backup v1.0.0 made when it
    updated to the pre-release, a GitHub that offers both, and a page that confirmed `case`."""

    def __init__(self, tmp_path, fake_runner, *steps, case):
        self.scripts = beta_scripts(tmp_path, *steps) if steps else None
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.db = self.data / "r.db"
        engine = make_engine(self.db)
        upgrade(engine, revision="0022")
        engine.dispose()
        self.order = sql(self.db, "INSERT INTO orders (reference, order_date) VALUES ('A-1', '2026-11-01') RETURNING id")[0][0]
        add_journal(self.db)
        add_journal(self.db)
        self.switch = backup_database(self.db, backup_dir_for(self.db), label=STABLE, now=BACKUP_AT)
        engine = make_engine(self.db)
        upgrade(engine, self.scripts)
        engine.dispose()
        sql(self.db, "INSERT INTO settings (key, value) VALUES ('update_channel', ?)", json.dumps("prerelease"))
        self.opt = tmp_path / "opt"
        self.opt.mkdir()
        write_marker(self.opt, tag=BETA, repo=("owner", "repo"), sha256="0" * 64)
        (self.opt / "releases" / BETA).mkdir(parents=True)
        (self.opt / "releases" / BETA / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (self.opt / ".venv").mkdir()
        (self.opt / ".venv" / "which").write_text("beta", encoding="utf-8")
        self.archives = tmp_path / "archives"
        self.archives.mkdir()
        self.runner = fake_runner
        self.py = str(venv_python(self.opt))
        fake_runner.on((self.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 1.0.0\n"))
        self.github = FakeGitHub((BETA, STABLE))
        self.downloads: list[str] = []
        self.on_download = None
        self.health = [True]
        self.queue(case)

    def queue(self, case, *, age=timedelta(seconds=30), head="0022", backup=None):
        write_status(
            self.data,
            UpdateStatus(
                state="queued", step="start", from_tag=BETA, to_tag=STABLE, started_at=NOW - age, kind="return",
                return_case=case, target_head=head,
                return_backup=str(backup or self.switch) if case == 3 else None,
            ),
        )

    def download(self, url, dest):
        self.downloads.append(url)
        if self.on_download is not None:
            self.on_download(url)
        name = url.rsplit("/", 1)[1]
        tag = name.removeprefix("reprintbuddy-").removesuffix(".sha256").removesuffix(".tar.gz")
        archive = self.archives / package_name(tag)
        if not archive.exists():
            build_archive(archive, tag)
        if name.endswith(".sha256"):
            dest.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {package_name(tag)}\n", encoding="utf-8")
        else:
            dest.write_bytes(archive.read_bytes())

    def wait_healthy(self):
        return self.health.pop(0) if len(self.health) > 1 else self.health[0]

    def run(self, **kwargs):
        return run_update(
            self.db, install_dir=self.opt, runner=self.runner, wait_healthy=self.wait_healthy, now=lambda: NOW,
            http_get=self.github, download=self.download, script_location=self.scripts, **kwargs,
        )

    def pip(self):
        return (self.py, "-m", "pip", "install")

    def index(self, prefix):
        return next(i for i, call in enumerate(self.runner.calls) if call[: len(prefix)] == prefix)

    def last_index(self, prefix):
        return max(i for i, call in enumerate(self.runner.calls) if call[: len(prefix)] == prefix)


# --- the three cases ---------------------------------------------------------------------------------


def test_case_1_switches_the_program_and_keeps_every_entry(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, case=1)
    before = content(env.db)
    result = env.run()
    assert (result.state, result.kind, result.from_tag, result.to_tag) == ("succeeded", "return", BETA, STABLE)
    assert env.index(STOP_CMD) < env.index(env.pip()) < env.last_index(START_CMD)
    assert RESTART_WAIT_CMD not in fake_runner.calls
    assert channel(env.db) == "stable"
    revision, schema, rows = content(env.db)
    rows.pop("settings"), before[2].pop("settings")
    assert (revision, schema, rows) == (before[0], before[1], before[2])
    assert read_installation(env.opt, fake_runner).tag == STABLE
    assert (env.opt / "releases" / STABLE).is_dir() and (env.opt / "releases" / BETA).is_dir()
    assert not (env.data / RETURN_DIR).exists()
    assert read_status(env.data).kind == "return"


def test_the_database_is_switched_before_pip(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, "add_table", case=2)
    seen = []
    fake_runner.on(env.pip(), lambda call: seen.append(current_revision(env.db)) or CommandResult(0))
    assert env.run().state == "succeeded"
    assert seen == ["0022"]


def test_case_2_removes_what_the_pre_release_added(tmp_path, fake_runner, capsys):
    env = ReturnEnv(tmp_path, fake_runner, "add_table", "add_column", case=2)
    for text in ("a", "b", "c"):
        sql(env.db, "INSERT INTO t_notes (order_id, text) VALUES (?, ?)", env.order, text)
    assert env.run().state == "succeeded"
    assert current_revision(env.db) == "0022"
    assert not sql(env.db, "SELECT name FROM sqlite_master WHERE name = 't_notes'")
    assert sql(env.db, "SELECT reference FROM orders") == [("A-1",)]
    assert sql(env.db, "SELECT count(*) FROM journal") == [(2,)]
    assert channel(env.db) == "stable"
    assert "Test notes: 3" in capsys.readouterr().out


def test_case_3_brings_back_the_backup_from_before_the_pre_release(tmp_path, fake_runner, capsys):
    env = ReturnEnv(tmp_path, fake_runner, "rework_marked", case=3)
    add_journal(env.db)
    assert env.run().state == "succeeded"
    assert current_revision(env.db) == "0022"
    assert sql(env.db, "SELECT reference FROM orders") == [("A-1",)]  # the reference before the rework
    assert sql(env.db, "SELECT count(*) FROM journal") == [(2,)]  # the booking since then is gone
    assert channel(env.db) == "stable"
    assert "1 journal entries" in capsys.readouterr().out


# --- a failure leaves the pre-release running with the data from before ---------------------------------


def fail_at(env, step, monkeypatch):
    if step == "stop":
        env.runner.on(STOP_CMD, CommandResult(1, "", "denied"))
    elif step == "database":
        monkeypatch.setattr(updater, "build_stable_db", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    elif step == "swap":
        real = updater.restore_database

        def restore(source, target):
            if source.name == "stable.db":
                raise sqlite3.OperationalError("disk I/O error")
            real(source, target)

        monkeypatch.setattr(updater, "restore_database", restore)
    elif step == "install":
        env.runner.on(env.pip(), CommandResult(1, "", "ERROR: no network"))
    elif step == "verify":
        env.runner.on((env.py, "-m", "reprintbuddy", "--version"), CommandResult(0, "reprintbuddy 1.1.0b1\n"))
    elif step == "migrate":
        env.runner.on((env.py, "-m", "reprintbuddy", "migrate"), CommandResult(1, "", "Error: unknown revision"))
    elif step == "restart":
        env.runner.on(START_CMD, CommandResult(1, "", "failed"), CommandResult(0))
    elif step == "health":
        env.health = [False, True]


@pytest.mark.parametrize(
    ("step", "state"),
    [
        ("stop", "failed"), ("database", "failed"), ("swap", "rolled_back"), ("install", "rolled_back"),
        ("verify", "rolled_back"), ("migrate", "rolled_back"), ("restart", "rolled_back"), ("health", "rolled_back"),
    ],
)
def test_a_failure_keeps_the_pre_release_with_its_data(tmp_path, fake_runner, monkeypatch, step, state):
    env = ReturnEnv(tmp_path, fake_runner, "add_table", case=2)
    sql(env.db, "INSERT INTO t_notes (order_id, text) VALUES (?, 'kept')", env.order)
    before = content(env.db)
    fail_at(env, step, monkeypatch)
    result = env.run()
    assert (result.state, result.kind) == (state, "return"), result.message
    assert content(env.db) == before
    assert channel(env.db) == "prerelease"
    assert read_installation(env.opt, fake_runner).tag == BETA
    assert (env.opt / ".venv" / "which").read_text(encoding="utf-8") == "beta"
    assert fake_runner.calls[-1] == START_CMD or START_CMD in fake_runner.calls[env.last_index(STOP_CMD):]
    assert not (env.data / RETURN_DIR).exists()


def test_a_failure_after_the_switch_restores_entries_made_before_the_stop(tmp_path, fake_runner):
    """Review focus 3: written between the first backup and the stop (the service still ran)."""
    env = ReturnEnv(tmp_path, fake_runner, "add_table", case=2)
    env.on_download = lambda url: add_journal(env.db) if url.endswith(".tar.gz") else None
    fake_runner.on(env.pip(), CommandResult(1, "", "ERROR: no network"))
    assert env.run().state == "rolled_back"
    assert sql(env.db, "SELECT count(*) FROM journal") == [(3,)]


def test_entries_made_before_the_stop_reach_the_stable_version(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, "add_table", case=2)
    env.on_download = lambda url: add_journal(env.db) if url.endswith(".tar.gz") else None
    assert env.run().state == "succeeded"
    assert sql(env.db, "SELECT count(*) FROM journal") == [(3,)]


def test_a_failed_rollback_names_the_database_as_it_was(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, "add_table", case=2)
    fake_runner.on(env.pip(), CommandResult(1, "", "ERROR: no network"))
    env.health = [False]
    result = env.run()
    assert result.state == "rollback_failed"
    kept = env.data / RETURN_DIR / "before.db"
    assert kept.is_file() and str(kept) in result.message


# --- a request that changed or grew old changes nothing -------------------------------------------------


def test_a_newer_stable_version_since_the_check_changes_nothing(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, case=1)
    env.github = FakeGitHub((BETA, "v1.0.1", STABLE))
    before = content(env.db)
    result = env.run()
    assert (result.state, result.reason, result.kind) == ("failed", "return_changed", "return")
    assert env.downloads == [] and STOP_CMD not in fake_runner.calls
    assert content(env.db) == before


def test_another_stable_head_since_the_check_changes_nothing(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, case=1)
    env.queue(1, head="0021")
    result = env.run()
    assert (result.state, result.reason) == ("failed", "return_changed")
    assert STOP_CMD not in fake_runner.calls
    assert not (env.opt / "releases" / STABLE).exists()


def test_another_case_since_the_check_changes_nothing(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, "add_table", case=1)
    before = content(env.db)
    result = env.run()
    assert (result.state, result.reason) == ("failed", "return_changed")
    assert content(env.db) == before and env.last_index(START_CMD) > env.index(STOP_CMD)


def test_a_backup_gone_since_the_check_changes_nothing(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, "rework_marked", case=3)
    env.switch.unlink()
    before = content(env.db)
    result = env.run()
    assert (result.state, result.reason) == ("failed", "return_changed")
    assert content(env.db) == before


def test_an_old_request_for_the_way_back_changes_nothing(tmp_path, fake_runner):
    """B3: never forward on a click on "back"; the next run by hand is an ordinary update again."""
    env = ReturnEnv(tmp_path, fake_runner, case=1)
    env.queue(1, age=timedelta(minutes=3))
    result = env.run()
    assert (result.state, result.reason, result.kind) == ("failed", "return_expired", "return")
    assert env.downloads == [] and fake_runner.calls == []
    assert read_status(env.data).reason == "return_expired"


def test_the_way_back_cannot_be_typed_on_the_command_line(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, case=1)
    write_status(env.data, UpdateStatus())
    assert env.run(target=STABLE).reason == "no_newer"
    env.queue(1)
    assert env.run(target=STABLE).reason == "no_newer"  # a target typed by hand is an ordinary update


def test_a_queued_request_of_another_kind_is_an_ordinary_update(tmp_path, fake_runner):
    env = ReturnEnv(tmp_path, fake_runner, case=1)
    write_status(env.data, UpdateStatus(state="queued", step="start", from_tag=BETA, to_tag=STABLE, started_at=NOW))
    assert env.run().reason == "no_newer"
