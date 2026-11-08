"""`reprintbuddy restore-backup <file>`: the emergency way to restore a full backup (p100, Spec 11.6).

When the service does not start any more (after a restore that went wrong, or a broken database),
the page for the restore cannot be reached. The same restore is then done on the command line:

- the same checks as in the interface, with the same functions (`fullbackup`): shape and manifest
  (is it ours, is it not from a newer version), password, integrity, revision, structure, a trial
  migration. The database of the backup is never used as it comes; its rows are rebuilt in a database
  of this version, exactly as the interface does.
- nothing is touched while the service runs: its database is open and the engine writes to it.
- the database that was there is saved first (`before-restore`, as for an update); a database that is
  missing or cannot be read is set aside under `backups/` (`damaged-<time>.db`, never pruned).
- the password is asked on the terminal (`getpass`): not an argument, not in the shell history, not
  in the environment, and never printed.

The service is found by two questions that need no rights: does systemd say the unit is active, does
anything answer on the saved address? A program of another kind on that port gives the same answer;
the message says so.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import socket
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from reprintbuddy import settings as st
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.ops.backup import _copy, backup_dir_for, connect_existing
from reprintbuddy.ops.runner import CommandRunner
from reprintbuddy.ops.server import connect_host
from reprintbuddy.ops.service import SERVICE, SYSTEMCTL

EXIT_OK = 0
EXIT_REFUSED = 1  # the file was looked at and cannot be restored (nothing was changed)
EXIT_NOT_DONE = 2  # asked wrongly, the file cannot be read, or the service runs (nothing was changed)

PASSWORD_TRIES = 3
# systemd's answers of `is-active` for a unit that is, or is about to be, running.
_ALIVE = frozenset({"active", "activating", "reloading", "deactivating", "refreshing"})

MESSAGES = {
    "foreign": "This is not a Reprintbuddy backup.",
    "too_new": "This backup comes from a newer version of Reprintbuddy. Update first, then restore.",
    "too_large": "The file is too large for a backup.",
    "damaged": "The backup is damaged.",
    "incompatible": "This backup does not hold a database this version of Reprintbuddy can use.",
    "wrong_password": "Wrong password.",
    "password_needed": "The backup is encrypted and needs its password.",
}


def listening_address(db_path: Path) -> tuple[str, int]:
    """Host and port the service listens on, from the database; the defaults if it cannot be read.

    Plain `sqlite3`, read only: this runs when the database may be broken.
    """
    host, port = str(st.DEFAULTS["host"]), int(st.DEFAULTS["port"])  # type: ignore[arg-type]
    try:
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            rows = dict(
                connection.execute("SELECT key, value FROM settings WHERE key IN ('host', 'port')").fetchall()
            )
        finally:
            connection.close()
        host = str(json.loads(rows["host"]) or host) if "host" in rows else host
        port = int(json.loads(rows["port"])) if "port" in rows else port
    except (sqlite3.Error, ValueError, TypeError, OSError, KeyError):
        pass
    return host, port


def service_running(
    db_path: Path,
    runner: CommandRunner,
    *,
    connect: Callable[..., socket.socket] = socket.create_connection,
) -> str | None:
    """Why the service seems to run (a sentence), or None when nothing says so.

    Two questions, both without rights: `systemctl is-active` (when systemd is there) and a
    connection to the saved address. A refused connection is a "no"; anything else that goes
    wrong (no route, a timeout) says nothing and is left out.
    """
    state = runner((SYSTEMCTL, "is-active", SERVICE), timeout=10.0)
    if state.stdout.strip() in _ALIVE:
        return f"systemd reports {SERVICE} as {state.stdout.strip()}"
    host, port = listening_address(db_path)
    for target in dict.fromkeys((connect_host(host), "::1" if ":" in host else "127.0.0.1")):
        try:
            with connect((target, port), timeout=2):
                return f"something answers on port {port}"
        except ConnectionRefusedError:
            continue
        except OSError:
            continue
    return None


def _say_when(manifest: fb.Manifest) -> str:
    stamp = manifest.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
    return f"made on {stamp} by Reprintbuddy {manifest.app_version}"


def _readable(db_path: Path) -> bool:
    """Can SQLite read the database in place? A garbage file answers `PRAGMA page_size` all the same
    (it needs no page), so the schema is read and a quick integrity check is made."""
    if not db_path.is_file():
        return False
    try:
        connection = connect_existing(db_path, timeout=5)
        try:
            connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            return connection.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def _install_unreadable(candidate: Path, db_path: Path, now: datetime) -> Path | None:
    """The database in place is missing or cannot be read: set it aside, put the candidate there.

    Returns where the old file went, None when there was none. The old file is moved, not copied
    (a broken file may not be copyable by SQLite) and its name matches no pattern of the pruning.
    If putting the new one in fails, the old file is moved back: a refusal changes nothing.
    """
    aside: Path | None = None
    leftovers = [Path(f"{db_path}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    if db_path.exists():
        folder = backup_dir_for(db_path)
        folder.mkdir(parents=True, exist_ok=True)
        aside = folder / f"damaged-{now:%Y%m%dT%H%M%SZ}.db"
        db_path.replace(aside)
        for leftover in leftovers:  # they belong to the file that was moved away
            if leftover.exists():
                leftover.replace(Path(f"{aside}{leftover.name[len(db_path.name):]}"))
    try:
        fb._prepare(candidate, fb._page_size(candidate))
        _copy(candidate, db_path)
    except BaseException:
        fb.remove_database_files(db_path)
        if aside is not None:
            aside.replace(db_path)
            for suffix in ("-wal", "-shm", "-journal"):
                kept = Path(f"{aside}{suffix}")
                if kept.exists():
                    kept.replace(Path(f"{db_path}{suffix}"))
        raise
    fb.remove_database_files(candidate)
    return aside


def restore_backup(
    archive: Path,
    db_path: Path,
    *,
    runner: CommandRunner,
    ask_password: Callable[[], str],
    say: Callable[[str], None],
    now: datetime,
    probe: Callable[[], str | None] | None = None,
    complain: Callable[[str], None] | None = None,
) -> int:
    """Restore `archive` into `db_path`; the exit code. Every refusal changes nothing.

    `say` gets the progress, `complain` (by default `say`) the reason of a refusal.
    """
    complain = complain or say

    def refuse(code: int, text: str) -> int:
        complain(f"reprintbuddy: {text}")
        complain("Nothing was changed.")
        return code

    try:
        with open(archive, "rb"):
            pass
    except FileNotFoundError:
        return refuse(EXIT_NOT_DONE, f"file not found: {archive}")
    except OSError as error:
        return refuse(
            EXIT_NOT_DONE,
            f"cannot read {archive} ({type(error).__name__}). The command runs as the service user, "
            "which must be able to read the file: put it where that user can, for example in /tmp.",
        )

    why = probe() if probe is not None else service_running(db_path, runner)
    if why:
        return refuse(
            EXIT_NOT_DONE,
            f"Reprintbuddy seems to be running ({why}). Stop it first: systemctl stop reprintbuddy. "
            "(Another program on the same port gives the same answer; stop or move that one, "
            "or change the port with 'reprintbuddy set-port'.)",
        )

    try:
        manifest = fb.read_manifest(archive)
    except fb.BackupError as error:
        return refuse(EXIT_REFUSED, MESSAGES.get(error.code, MESSAGES["foreign"]))
    except OSError as error:
        return refuse(EXIT_NOT_DONE, f"cannot read {archive} ({type(error).__name__}).")

    older = False
    with contextlib.suppress(Exception):
        from reprintbuddy.db import known_revisions

        older = manifest.schema_revision != known_revisions()[-1]
    say(f"Backup {_say_when(manifest)}, {'encrypted' if manifest.encrypted else 'not encrypted'}.")
    if older:
        say("It is from an older version; the database is brought up to date when the service starts.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    work = fb.ensure_work_dir(fb.work_dir_for(db_path))
    candidate = work / f"candidate-{secrets.token_hex(8)}.db"
    try:
        tries = PASSWORD_TRIES if manifest.encrypted else 1
        for attempt in range(1, tries + 1):
            password = ask_password() if manifest.encrypted else None
            try:
                fb.extract_database(archive, manifest, password, candidate)
                break
            except (fb.PasswordNeeded, fb.WrongPassword) as error:
                if attempt == tries:
                    return refuse(EXIT_REFUSED, MESSAGES[error.code])
                say(MESSAGES[error.code])
        say("Checking the database ...")
        fb.verify_database(candidate, manifest, work_dir=work)
    except fb.BackupError as error:
        fb.remove_database_files(candidate)
        return refuse(EXIT_REFUSED, MESSAGES.get(error.code, MESSAGES["foreign"]))
    except Exception as error:  # noqa: BLE001 - a full disk in a trial migration, say: nothing was changed
        fb.remove_database_files(candidate)
        return refuse(EXIT_REFUSED, f"the check failed ({type(error).__name__}).")

    try:
        if _readable(db_path):
            before = fb.replace_database(candidate, db_path, now=now)
        else:
            before = _install_unreadable(candidate, db_path, now)
    except Exception as error:  # noqa: BLE001 - before or in the one-step copy
        fb.remove_database_files(candidate)
        return refuse(EXIT_REFUSED, f"the restore failed ({type(error).__name__}).")

    say(f"Restored the backup {_say_when(manifest)}.")
    if before is not None:
        say(f"The database that was there was saved as {before}.")
    else:
        say("There was no database to save.")
    say("Everybody has to sign in again, with the account of the backup.")
    say("Now start Reprintbuddy: systemctl start reprintbuddy")
    return EXIT_OK
