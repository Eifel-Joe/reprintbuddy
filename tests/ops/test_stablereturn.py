"""Which way back a pre-release takes, and what it costs (Spec 11.3a "Rückweg zur stabilen Version").

Case 1: the database stands on the head of the stable version, only the program changes.
Case 2: every step after it only adds; the downgrade, rehearsed on a copy, removes exactly that, and the
page names the steps that hold entries. Case 3: a step is (or counts as) rework, the rehearsal fails, or the
stable head is not in the history of the pre-release; the backup from before the pre-release comes back and
the page names what is lost since. Without that backup there is no way back.
"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from beta_steps import beta_scripts, revision_of
from package_support import build_archive

from reprintbuddy.db import MIGRATIONS_DIR, make_engine, upgrade
from reprintbuddy.ops.backup import backup_database, backup_dir_for
from reprintbuddy.ops.migrationsteps import current_revision, graph_of_dir
from reprintbuddy.ops.package import PackageError
from reprintbuddy.ops.releases import Release, checksum_name, package_name
from reprintbuddy.ops.stablereturn import Loss, assess, build_stable_db, entries_since, prepare_target

STABLE = graph_of_dir(MIGRATIONS_DIR / "versions")  # head 0022, as the package of v1.0.0 carries it
BACKUP_AT = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)


def sql(path, statement, *args):
    con = sqlite3.connect(path)
    try:
        rows = con.execute(statement, args).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def add_order(path, reference="A-1"):
    return sql(path, "INSERT INTO orders (reference, order_date) VALUES (?, '2026-11-01') RETURNING id", reference)[0][0]


def add_journal(path, n=1):
    for _ in range(n):
        sql(path, "INSERT INTO journal (at, booked_at, actor, kind, qty) VALUES ('2026-11-12 10:00:00', '2026-11-12 10:00:00', 'user', 'stock_in', 1)")


def add_activity(path, n=1):
    for _ in range(n):
        sql(path, "INSERT INTO activity (at, area, kind, actor) VALUES ('2026-11-12 10:00:00', 'ops', 'x', 'user')")


class Beta:
    """A data folder whose database stands on a pre-release with `steps` after the stable head 0022."""

    def __init__(self, tmp_path: Path, *steps: str):
        self.scripts = beta_scripts(tmp_path, *steps) if steps else None
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.db = self.data / "r.db"
        self.work = self.data / ".return"
        engine = make_engine(self.db)
        upgrade(engine, revision="0022")
        engine.dispose()
        self.order = add_order(self.db)
        add_journal(self.db, 2)
        add_activity(self.db, 1)
        self.backup = backup_database(self.db, backup_dir_for(self.db), label="v1.0.0", now=BACKUP_AT)
        engine = make_engine(self.db)
        upgrade(engine, self.scripts)
        engine.dispose()

    def assess(self, target_graph=STABLE):
        return assess(
            self.db, target="v1.0.0", target_graph=target_graph, backup_dir=backup_dir_for(self.db),
            work_dir=self.work, script_location=self.scripts,
        )


def test_case_1_when_the_database_stands_on_the_stable_head(tmp_path):
    plan = Beta(tmp_path).assess()
    assert (plan.case, plan.target, plan.target_head, plan.db_revision) == (1, "v1.0.0", "0022", "0022")
    assert plan.losses == () and plan.unavailable is None


def test_case_2_names_the_steps_with_entries(tmp_path):
    beta = Beta(tmp_path, "add_table", "add_column")
    for text in ("a", "b", "c"):
        sql(beta.db, "INSERT INTO t_notes (order_id, text) VALUES (?, ?)", beta.order, text)
    plan = beta.assess()
    assert plan.case == 2
    assert plan.losses == (Loss({"de": "Testnotizen", "en": "Test notes"}, 3),)


def test_case_2_counts_values_in_new_columns_and_leaves_out_steps_without_entries(tmp_path):
    beta = Beta(tmp_path, "add_table", "add_column")
    second = add_order(beta.db, "A-2")
    sql(beta.db, "UPDATE orders SET t_flag = 1 WHERE id IN (?, ?)", beta.order, second)
    plan = beta.assess()
    assert plan.losses == (Loss({"de": "Testmarken an Bestellungen", "en": "Test marks on orders"}, 2),)


def test_case_2_without_entries_loses_nothing(tmp_path):
    plan = Beta(tmp_path, "add_table").assess()
    assert (plan.case, plan.losses) == (2, ())


def test_the_rehearsal_leaves_the_database_and_no_copy_behind(tmp_path):
    beta = Beta(tmp_path, "add_table")
    beta.assess()
    assert current_revision(beta.db) == revision_of(1)
    assert list(beta.work.iterdir()) == [] if beta.work.exists() else True


@pytest.mark.parametrize(("steps", "why"), [(("rework_marked",), "rework"), (("add_table", "no_note"), "rework"), (("raising_downgrade",), "trial_failed")])
def test_case_3_for_rework_unmarked_steps_and_a_failed_rehearsal(tmp_path, steps, why):
    beta = Beta(tmp_path, *steps)
    add_journal(beta.db, 2)
    add_activity(beta.db, 3)
    plan = beta.assess()
    assert (plan.case, plan.why3) == (3, why)
    assert plan.backup == beta.backup and plan.backup_at == BACKUP_AT
    assert len(plan.journal_ids) == 2 and plan.journal_ids[0] > plan.journal_ids[1]
    assert plan.activity_since == 3


def test_case_3_when_the_stable_head_is_not_in_the_history_of_the_pre_release(tmp_path):
    beta = Beta(tmp_path, "add_table")
    other = dict(STABLE) | {"0023x": "0022"}  # a stable version with a step this pre-release does not know
    plan = beta.assess(other)
    assert (plan.case, plan.why3, plan.target_head) == (3, "not_in_history", "0023x")


def test_no_way_back_without_the_backup_from_before_the_pre_release(tmp_path):
    beta = Beta(tmp_path, "rework_marked")
    beta.backup.unlink()
    plan = beta.assess()
    assert (plan.case, plan.unavailable) == (None, "no_backup")


def test_no_way_back_with_a_backup_the_stable_version_does_not_know(tmp_path):
    beta = Beta(tmp_path, "rework_marked")
    sql(beta.backup, "UPDATE alembic_version SET version_num = 'zzzz'")
    plan = beta.assess()
    assert (plan.case, plan.unavailable) == (None, "backup_unusable")


def test_a_backup_on_an_older_step_of_the_stable_version_will_do(tmp_path):
    beta = Beta(tmp_path, "rework_marked")
    sql(beta.backup, "UPDATE alembic_version SET version_num = '0021'")
    assert beta.assess().case == 3


def test_entries_since_a_backup(tmp_path):
    beta = Beta(tmp_path)
    add_journal(beta.db, 3)
    add_activity(beta.db, 2)
    ids, activity = entries_since(beta.db, beta.backup)
    assert ids == (5, 4, 3) and activity == 2


def channel(path):
    rows = sql(path, "SELECT value FROM settings WHERE key = 'update_channel'")
    return json.loads(rows[0][0]) if rows else None


def tables(path):
    return {row[0] for row in sql(path, "SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_build_case_2_removes_what_was_added_and_switches_the_channel(tmp_path):
    beta = Beta(tmp_path, "add_table", "add_column")
    sql(beta.db, "INSERT INTO settings (key, value) VALUES ('update_channel', ?)", json.dumps("prerelease"))
    sql(beta.db, "INSERT INTO t_notes (order_id, text) VALUES (?, 'a')", beta.order)
    out = build_stable_db(beta.assess(), beta.db, tmp_path / "stable.db", script_location=beta.scripts)
    assert current_revision(out) == "0022"
    assert "t_notes" not in tables(out)
    assert sql(out, "SELECT reference FROM orders") == [("A-1",)]
    assert channel(out) == "stable"
    assert channel(beta.db) == "prerelease"  # the live database is not touched


def test_build_case_3_is_the_backup_with_the_channel_switched(tmp_path):
    beta = Beta(tmp_path, "rework_marked")
    add_journal(beta.db, 4)
    out = build_stable_db(beta.assess(), beta.db, tmp_path / "stable.db", script_location=beta.scripts)
    assert sql(out, "SELECT count(*) FROM journal") == [(2,)]
    assert sql(out, "SELECT reference FROM orders") == [("A-1",)]  # not in capitals: the backup
    assert channel(out) == "stable"


def test_build_case_1_is_a_copy_with_the_channel_switched(tmp_path):
    beta = Beta(tmp_path)
    out = build_stable_db(beta.assess(), beta.db, tmp_path / "stable.db")
    assert current_revision(out) == "0022" and channel(out) == "stable"


def test_build_refuses_a_plan_without_a_way_back(tmp_path):
    beta = Beta(tmp_path, "rework_marked")
    beta.backup.unlink()
    with pytest.raises(ValueError):
        build_stable_db(beta.assess(), beta.db, tmp_path / "stable.db", script_location=beta.scripts)


# --- the package of the stable version ---------------------------------------------------------------


def stable_release(tmp_path, *, checksum=None):
    archive = build_archive(tmp_path / "src.tar.gz", "v1.0.0")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    files = {
        package_name("v1.0.0"): archive.read_bytes(),
        checksum_name("v1.0.0"): (checksum or f"{digest}  {package_name('v1.0.0')}\n").encode(),
    }

    def download(url, dest):
        dest.write_bytes(files[url.rsplit("/", 1)[1]])

    base = "https://github.com/owner/repo/releases/download/v1.0.0/"
    release = Release("v1.0.0", False, None, "", base + package_name("v1.0.0"), base + checksum_name("v1.0.0"), None)
    return release, download


def test_the_target_steps_come_from_the_checked_package(tmp_path):
    release, download = stable_release(tmp_path)
    graph = prepare_target(release, download, tmp_path / "work")
    assert graph == STABLE
    assert not (tmp_path / "work").exists()


def test_a_package_that_does_not_match_its_checksum_is_refused(tmp_path):
    release, download = stable_release(tmp_path, checksum=f"{'0' * 64}  {package_name('v1.0.0')}\n")
    with pytest.raises(PackageError):
        prepare_target(release, download, tmp_path / "work")
    assert not (tmp_path / "work").exists()


def test_a_column_default_is_compared_as_a_value_not_as_sql(tmp_path):
    """Review finding 3: a default is evaluated once and compared as a value."""
    from reprintbuddy.ops.stablereturn import _count

    con = sqlite3.connect(tmp_path / "d.db")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, mark TEXT DEFAULT 'x', n INTEGER DEFAULT (1 + 1))")
    con.executemany("INSERT INTO t (mark, n) VALUES (?, ?)", [("x", 2), ("y", 2), ("x", 5), (None, None)])
    assert _count(con, "t", "mark", "'x'") == 1
    assert _count(con, "t", "n", "(1 + 1)") == 1
    assert _count(con, "t", "mark", None) == 3
    assert _count(con, "t", "mark", "no_such_column") == 3  # not a value: counted as without a default
    con.close()


# --- p610: the start after a way back that was cut off (Spec 11.3a "Rückweg", Ablauf) ---------------------------
# A power cut, a hard restart or SIGKILL between the switch of the database and the end of pip leaves the
# database of the stable version under the pre-release. Before the pre-release opens the database it puts back
# the one kept when the service stopped (`.return/before.db`), and only then.

CUT_AT = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
BETA_TAG = "v1.2.0-beta.2"


class CutOff:
    """The live database as the stable version wants it, `before.db` as it was, a status of a way back cut off."""

    def __init__(self, tmp_path, *, state="running", step="install", kind="return", reason="", before=True):
        from reprintbuddy.ops.status import UpdateStatus, write_status

        self.data = tmp_path / "data"
        self.data.mkdir()
        self.db = self.data / "r.db"
        engine = make_engine(self.db)
        upgrade(engine)
        engine.dispose()
        add_journal(self.db, 3)
        self.before = self.data / ".return" / "before.db"
        self.before.parent.mkdir()
        if before:
            backup_database(self.db, self.data / "tmp-backups", label="x", now=BACKUP_AT).rename(self.before)
        sql(self.db, "DELETE FROM journal")  # what the stable database lacks
        write_status(
            self.data,
            UpdateStatus(
                state=state, step=step, from_tag=BETA_TAG, to_tag="v1.1.0", started_at=CUT_AT, kind=kind,
                return_case=2, target_head="0022", reason=reason, backup=str(self.data / "backups" / "b.db"),
            ),
        )

    def recover(self, version="1.2.0-beta.2"):
        from reprintbuddy.ops.stablereturn import recover_cut_off_return

        return recover_cut_off_return(self.db, version, CUT_AT.replace(minute=5))

    def journal(self):
        return sql(self.db, "SELECT count(*) FROM journal")[0][0]


@pytest.mark.parametrize("step", ["swap", "install", "verify", "migrate", "restart", "health", "rollback"])
def test_p610_a_way_back_cut_off_after_the_switch_is_put_back_at_the_start(tmp_path, caplog, step):
    from reprintbuddy.ops.status import read_status

    env = CutOff(tmp_path, step=step)
    with caplog.at_level("WARNING"):
        result = env.recover()
    assert env.journal() == 3  # the database as it was when the service stopped
    assert not (env.data / ".return").exists()
    status = read_status(env.data)
    assert result == status
    assert (status.state, status.reason, status.kind, status.step) == ("rolled_back", "interrupted_recovered", "return", "rollback")
    assert ": interrupted" in status.message  # the log says "interrupted"
    assert status.finished_at == CUT_AT.replace(minute=5)
    assert any("before.db" in record.getMessage() for record in caplog.records)  # a line in the system log


def test_p610_a_failed_rollback_after_a_cut_off_is_not_put_back(tmp_path):
    """Review finding 1: the helper had its time; it started the pre-release on the switched database (the card
    names `before.db`). Putting it back at a later start would lose what was entered since."""
    env = CutOff(tmp_path, state="rollback_failed", step="rollback", reason="interrupted_partial")
    assert env.recover() is None and env.journal() == 0


def test_p610_a_cut_off_after_the_pre_release_was_started_on_the_switched_database_is_not_put_back(tmp_path):
    """The helper died (SIGKILL) after it started the pre-release with `before.db` not back in place."""
    from reprintbuddy.ops.stablereturn import STARTED_MARK

    env = CutOff(tmp_path, step="rollback")
    (env.data / ".return" / STARTED_MARK).write_text("", encoding="utf-8")
    assert env.recover() is None and env.journal() == 0


@pytest.mark.parametrize(
    "change",
    [
        {"version": "1.1.0"},  # the stable version runs: program and database fit, nothing to do
        {"step": "database"},  # before the switch: the live database is untouched
        {"step": "stop"},
        {"kind": "update"},
        {"state": "succeeded"},
        {"state": "rolled_back"},
        {"state": "rollback_failed", "reason": "install_failed"},  # had its time; the card names the file
        {"before": False},
    ],
)
def test_p610_nothing_is_put_back_unless_every_condition_holds(tmp_path, change):
    from reprintbuddy.ops.status import read_status

    version = change.pop("version", "1.2.0-beta.2")
    env = CutOff(tmp_path, **change)
    status = read_status(env.data)
    assert env.recover(version) is None
    assert env.journal() == 0
    assert read_status(env.data) == status


def test_p610_nothing_is_put_back_while_a_helper_holds_the_lock(tmp_path):
    """The helper itself starts the pre-release in its rollback; it holds the lock all along."""
    from reprintbuddy.ops.oplock import acquire

    env = CutOff(tmp_path)
    lock = acquire(env.data, "update")
    try:
        assert env.recover() is None
    finally:
        lock.release()
    assert env.journal() == 0 and env.before.is_file()


def test_p610_a_failure_while_putting_back_leaves_everything_and_does_not_raise(tmp_path, monkeypatch, caplog):
    from reprintbuddy.ops import stablereturn
    from reprintbuddy.ops.status import read_status

    env = CutOff(tmp_path)
    status = read_status(env.data)

    def broken(source, target):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(stablereturn, "restore_database", broken)
    with caplog.at_level("WARNING"):
        assert env.recover() is None
    assert env.before.is_file() and read_status(env.data) == status
    assert any(str(env.before) in record.getMessage() for record in caplog.records)
