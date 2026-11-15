"""The way back from a pre-release to the stable version: which case, what it costs, the stable database
(Spec 11.3a "Rückweg zur stabilen Version").

The same functions decide twice: when the user asks for the way back (the confirmation page shows the case
and what is lost) and in the update helper right before anything changes (`updater._run_return`); a different
answer there stops the way back with nothing changed.

- Case 1, the database stands on the head of the stable version's steps: only the program changes.
- Case 2, every step after that head only adds (Spec 11.3a "nur hinzufügen", checked in CI): the downgrade of
  those steps removes exactly what they added. It is rehearsed on a copy; the entries in the new tables and
  columns are counted per step, and only steps with entries are named.
- Case 3, a step is marked as rework or carries no mark, the rehearsal fails, or the stable head is not in the
  history of this pre-release: the backup from before the switch to the pre-release comes back; the entries
  since then are lost and named (journal bookings listed, log entries counted). Without that backup, or with
  one the stable version cannot read, there is no way back (`unavailable`).

The head of the stable version comes from its downloaded and checked package (`prepare_target`), parsed,
never imported. Everything here works on copies; the live database is only read. `build_stable_db` writes the
database the stable version will run on, with the switch "include pre-releases" off (Spec 11.3a: otherwise the
same pre-release would be offered again at once).

All imports are at the top and all Alembic work happens before pip (see `updater.py`): pip replaces the
package files while the helper runs, and on the way back to 1.0.0 this module is gone afterwards.
Tests: tests/ops/test_stablereturn.py.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from reprintbuddy.ops.backup import backup_time, connect_existing, restore_database, switch_backup
from reprintbuddy.ops.migrationsteps import (
    StepInfo,
    current_revision,
    downgrade,
    graph_of_package,
    in_line,
    single_head,
    steps_between,
)
from reprintbuddy.ops.oplock import Held, acquire
from reprintbuddy.ops.package import Download, expected_sha256, verify
from reprintbuddy.ops.releases import Release, checksum_name, package_name
from reprintbuddy.ops.status import UpdateStatus, read_status, write_status

Case = Literal[1, 2, 3]
Unavailable = Literal["no_backup", "backup_unusable", "db_unreadable"]
Why3 = Literal["rework", "trial_failed", "not_in_history"]

TRIAL_SOURCE = "return-source.db"
TRIAL_COPY = "return-trial.db"
# Beside the database while the way back runs: `before.db`, the database as it was when the service stopped, and
# `stable.db`, the one the stable version will run on (`updater._run_return`).
RETURN_DIR = ".return"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Loss:
    note: dict[str, str]  # what the step adds, "de" and "en" (its `return_note`)
    count: int  # entries in its new tables and columns


@dataclass(frozen=True)
class ReturnPlan:
    target: str
    target_head: str
    db_revision: str | None
    case: Case | None  # None: no way back, `unavailable` says why
    unavailable: Unavailable | None = None
    why3: Why3 | None = None
    losses: tuple[Loss, ...] = ()  # case 2, oldest step first
    backup: Path | None = None  # case 3
    backup_at: datetime | None = None
    journal_ids: tuple[int, ...] = ()  # case 3: journal entries since the backup, newest first
    activity_since: int = 0  # case 3: log entries since the backup


def copy_database(source: Path, target: Path) -> Path:
    """A consistent copy with SQLite's backup API (a plain file copy misses what still sits in the WAL)."""
    with contextlib.suppress(FileNotFoundError):
        target.unlink()
    src = connect_existing(source)
    try:
        dst = sqlite3.connect(str(target), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _shape(con: sqlite3.Connection) -> dict[str, dict[str, str | None]]:
    """Table -> column -> its default (SQL text, None for no default)."""
    tables = [
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    ]
    return {
        table: {row[1]: row[4] for row in con.execute(f"PRAGMA table_xinfo({_quote(table)})")} for table in tables
    }


def _count(con: sqlite3.Connection, table: str, column: str | None, default: str | None) -> int:
    """Rows of a table (column None), or rows whose value in a column differs from its default."""
    if column is None:
        return int(con.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0])
    condition, values = f"{_quote(column)} IS NOT NULL", []
    if default is not None:
        # The default, evaluated once and compared as a value (review: a default that is no value at all counts as
        # no default). A default by time counts every row: over, never under.
        try:
            values = [con.execute(f"SELECT ({default})").fetchone()[0]]
        except sqlite3.Error:
            values = []
        if values and values[0] is not None:
            condition += f" AND {_quote(column)} IS NOT ?"
        else:
            values = []
    return int(con.execute(f"SELECT count(*) FROM {_quote(table)} WHERE {condition}", values).fetchone()[0])


def trial(
    db_path: Path, target_head: str, steps: list[StepInfo], work_dir: Path, script_location: Path | None = None
) -> tuple[Loss, ...]:
    """Rehearse the downgrade of `steps` (newest first, down to `target_head`) on a copy and count, per step, the
    entries in what it removes. Raises whatever the downgrade raises. The copies are always removed."""
    work_dir.mkdir(parents=True, exist_ok=True)
    source, copy = work_dir / TRIAL_SOURCE, work_dir / TRIAL_COPY
    try:
        copy_database(db_path, source)
        copy_database(source, copy)
        removed: list[tuple[StepInfo, list[tuple[str, str | None, str | None]]]] = []
        downs = [step.revision for step in steps[1:]] + [target_head]
        for step, down in zip(steps, downs):
            con = sqlite3.connect(copy)
            try:
                before = _shape(con)
            finally:
                con.close()
            downgrade(copy, down, script_location)
            con = sqlite3.connect(copy)
            try:
                after = _shape(con)
            finally:
                con.close()
            gone: list[tuple[str, str | None, str | None]] = []
            for table, columns in before.items():
                if table not in after:
                    gone.append((table, None, None))
                    continue
                gone += [(table, column, default) for column, default in columns.items() if column not in after[table]]
            removed.append((step, gone))
        con = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        try:
            losses = []
            for step, gone in reversed(removed):  # oldest step first
                count = sum(_count(con, table, column, default) for table, column, default in gone)
                if count and step.note is not None:
                    losses.append(Loss(step.note, count))
        finally:
            con.close()
        return tuple(losses)
    finally:
        for path in (source, copy):
            for leftover in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
                with contextlib.suppress(OSError):
                    leftover.unlink()


def _max_id(con: sqlite3.Connection, table: str) -> int:
    try:
        return int(con.execute(f"SELECT coalesce(max(id), 0) FROM {_quote(table)}").fetchone()[0])
    except sqlite3.OperationalError:  # a backup from before the table existed
        return 0


def entries_since(db_path: Path, backup: Path) -> tuple[tuple[int, ...], int]:
    """(ids of the journal entries made since the backup, newest first; number of log entries since).

    By id, not by time: journal entries are never deleted, and the ids of both tables only grow."""
    old = sqlite3.connect(f"{Path(backup).resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        last_journal, last_activity = _max_id(old, "journal"), _max_id(old, "activity")
    finally:
        old.close()
    live = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        ids = tuple(row[0] for row in live.execute("SELECT id FROM journal WHERE id > ? ORDER BY id DESC", (last_journal,)))
        activity = int(live.execute("SELECT count(*) FROM activity WHERE id > ?", (last_activity,)).fetchone()[0])
    finally:
        live.close()
    return ids, activity


def assess(
    db_path: Path,
    *,
    target: str,
    target_graph: dict[str, str | None],
    backup_dir: Path,
    work_dir: Path,
    script_location: Path | None = None,
) -> ReturnPlan:
    """Which way back, and what it costs. `target_graph` holds the steps of the stable version (its package)."""
    target_head = single_head(target_graph)
    revision = current_revision(db_path)
    plan = ReturnPlan(target=target, target_head=target_head, db_revision=revision, case=None)
    if revision is None:
        return _replace(plan, unavailable="db_unreadable")
    if revision == target_head:
        return _replace(plan, case=1)
    steps = steps_between(target_head, revision, script_location)
    if steps is None:
        return _case_3(plan, "not_in_history", db_path, backup_dir, target_graph)
    if any(step.rework is not False or step.note is None for step in steps):
        return _case_3(plan, "rework", db_path, backup_dir, target_graph)
    try:
        losses = trial(db_path, target_head, steps, work_dir, script_location)
    except Exception:  # noqa: BLE001 - any failure of the rehearsal means: not this way (Spec 11.3a)
        return _case_3(plan, "trial_failed", db_path, backup_dir, target_graph)
    return _replace(plan, case=2, losses=losses)


def _replace(plan: ReturnPlan, **changes: object) -> ReturnPlan:
    values = {name: getattr(plan, name) for name in plan.__dataclass_fields__}
    values.update(changes)
    return ReturnPlan(**values)  # type: ignore[arg-type]


def _case_3(
    plan: ReturnPlan, why: Why3, db_path: Path, backup_dir: Path, target_graph: dict[str, str | None]
) -> ReturnPlan:
    backup = switch_backup(backup_dir)
    if backup is None:
        return _replace(plan, why3=why, unavailable="no_backup")
    revision = current_revision(backup)
    if revision is None or not in_line(target_graph, revision, plan.target_head):
        return _replace(plan, why3=why, unavailable="backup_unusable", backup=backup)
    try:
        ids, activity = entries_since(db_path, backup)
    except sqlite3.Error:
        return _replace(plan, why3=why, unavailable="backup_unusable", backup=backup)
    return _replace(
        plan, case=3, why3=why, backup=backup, backup_at=backup_time(backup), journal_ids=ids, activity_since=activity
    )


def set_stable_channel(path: Path) -> None:
    """The switch "include pre-releases" off (Spec 11.3a), in the database the stable version will run on."""
    con = connect_existing(path)
    try:
        con.execute(
            "INSERT INTO settings (key, value) VALUES ('update_channel', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps("stable"),),
        )
        con.commit()
    finally:
        con.close()


def build_stable_db(plan: ReturnPlan, db_path: Path, out: Path, script_location: Path | None = None) -> Path:
    """Write the database the stable version will run on to `out`; the live database is only read.

    Case 1 a copy, case 2 a copy taken back down to the stable head, case 3 a copy of the backup from before
    the pre-release; each with the switch "include pre-releases" off."""
    if plan.case is None:
        raise ValueError(f"no way back: {plan.unavailable}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if plan.case == 3:
        if plan.backup is None:
            raise ValueError("case 3 without a backup")
        copy_database(plan.backup, out)
    else:
        copy_database(db_path, out)
        if plan.case == 2:
            downgrade(out, plan.target_head, script_location)
    if current_revision(out) is None:
        raise ValueError("the stable database names no revision")
    set_stable_channel(out)
    con = sqlite3.connect(out)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("the stable database fails its integrity check")
    finally:
        con.close()
    return out


def prepare_target(release: Release, download: Download, work_dir: Path) -> dict[str, str | None]:
    """Download and check the package of the stable version and read its steps; `work_dir` goes afterwards.

    Raises `PackageError` (missing, unusable, checksum) or `StepsError` (no steps, several heads)."""
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    try:
        package = work_dir / package_name(release.tag)
        checksum = work_dir / checksum_name(release.tag)
        download(release.package_url, package)
        download(release.checksum_url, checksum)
        expected = expected_sha256(checksum.read_text(encoding="utf-8", errors="replace"), package_name(release.tag))
        verify(package, expected, release.digest)
        return graph_of_package(package, release.tag)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# p610 (Spec 11.3a "Rückweg", Ablauf): the steps from the switch of the live database on. A way back cut off in
# one of them (a power cut, a hard restart, SIGKILL: no time for the rollback) left the stable version's database
# under the pre-release; "rollback" is the rollback itself, cut off before it put `before.db` back.
_SWITCHED_STEPS = frozenset({"swap", "install", "verify", "migrate", "restart", "health", "rollback"})


def _cut_off_after_the_switch(status: UpdateStatus, version: str) -> bool:
    """A way back of THIS pre-release, cut off after the switch and never finished: the helper died ("running"), or
    its rollback after a SIGTERM could not put `before.db` back ("rollback_failed", "interrupted_partial")."""
    return (
        status.kind == "return"
        and status.step in _SWITCHED_STEPS
        and (status.state == "running" or (status.state == "rollback_failed" and status.reason == "interrupted_partial"))
        and status.from_tag == "v" + version
    )


def recover_cut_off_return(db_path: Path, version: str, now: datetime) -> UpdateStatus | None:
    """At the start of the pre-release, before it opens the database: put back the database kept when the service
    stopped, if a way back was cut off after the switch. Returns the new final state, or None (nothing done).

    Root (p610): the rollback of the way back needs time the helper does not always get (power cut, hard restart,
    SIGKILL). systemd starts the service at boot; the pre-release then ran on the database of the stable version
    and raised it to its own steps (`create_app` -> `upgrade`): in case 2 without the entries of its new tables,
    in case 3 without everything since the backup. Those entries lie in `.return/before.db`.
    Only when everything holds: the status says so (`_cut_off_after_the_switch`), `before.db` is there, no helper
    holds the lock (it puts the database back itself), and the version running is the pre-release the way back
    started from (the stable version runs on the stable database: they fit). `.return` goes before the final state
    is written: a cut between the two leaves a "running" without `before.db`, which is never put back twice.
    A failure changes nothing and stops nothing; `before.db` stays, the card and the system log name it.
    Rejected: doing it in the helper (it is gone; nothing starts it at boot), or at every start without the lock
    (a running helper may be using `before.db`). Tests: tests/ops/test_stablereturn.py "p610"."""
    data_dir = db_path.parent
    before = data_dir / RETURN_DIR / "before.db"
    if not before.is_file() or not _cut_off_after_the_switch(read_status(data_dir), version):
        return None
    try:
        lock = acquire(data_dir, "update")
    except (Held, OSError):
        return None
    try:
        status = read_status(data_dir)  # again, under the lock: a helper may have just finished
        if not before.is_file() or not _cut_off_after_the_switch(status, version):
            return None
        try:
            restore_database(before, db_path)
        except (OSError, sqlite3.Error) as error:
            log.warning(
                "way back: cut off at %s; the database as it was could not be put back (%s); it stays at %s",
                status.step, error, before,
            )
            return None
        log.warning(
            "way back: cut off at %s; the pre-release put back the database as it was before (%s)", status.step, before
        )
        shutil.rmtree(data_dir / RETURN_DIR, ignore_errors=True)
        final = replace(
            status,
            state="rolled_back",
            step="rollback",
            finished_at=now,
            message=f"{status.step}: interrupted, the pre-release put back the database from before the way back",
            reason="interrupted_recovered",
            params={},
            acknowledged=False,
        )
        try:
            write_status(data_dir, final)
        except OSError as error:
            log.warning("way back: cannot write the status file (%s)", error)
        return final
    finally:
        lock.release()
