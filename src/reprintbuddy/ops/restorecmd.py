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
import os
import secrets
import socket
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from reprintbuddy import settings as st
from reprintbuddy.activity.writer import current_marks, current_notify_marks, write_restored_marker
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.ops import oplock
from reprintbuddy.ops.backup import _copy, backup_dir_for, connect_existing
from reprintbuddy.ops.runner import CommandRunner
from reprintbuddy.ops.server import connect_host
from reprintbuddy.ops.service import SERVICE, SYSTEMCTL, UPDATE_SERVICE

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

    Three questions, all without rights: `systemctl is-active` for the service and for the update
    unit (an update that rolls back stops the service and restores its own copy of the database, so
    a restore in between would be overwritten), and a connection to the saved address. A refused
    connection is a "no"; anything else that goes wrong (no route, a timeout) says nothing and is
    left out.
    """
    for unit in (SERVICE, UPDATE_SERVICE):
        state = runner((SYSTEMCTL, "is-active", unit), timeout=10.0)
        if state.stdout.strip() in _ALIVE:
            return f"systemd reports {unit} as {state.stdout.strip()}"
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


class RestoreNotUndone(Exception):
    """Putting the new database in failed AND the old file could not be moved back: it lies at `where`."""

    def __init__(self, where: Path) -> None:
        super().__init__(str(where))
        self.where = where


def _private_folder(folder: Path) -> None:
    if os.name == "posix":
        folder.chmod(0o700)


def _private_file(path: Path) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(0o600)


def _install_unreadable(candidate: Path, db_path: Path, now: datetime) -> Path | None:
    """The database in place is missing or cannot be read: set it aside, put the candidate there.

    Returns where the old file went, None when there was none. The old file is moved, not copied
    (a broken file may not be copyable by SQLite) and its name matches no pattern of the pruning.
    If putting the new one in fails, the old file is moved back: a refusal changes nothing. If even
    that fails, `RestoreNotUndone` says where the old file is.
    """
    suffixes = ("-wal", "-shm", "-journal")
    aside: Path | None = None
    moved: list[tuple[Path, Path]] = []  # (where it was, where it is now), the main file first
    installing = False
    try:
        if db_path.exists():
            folder = backup_dir_for(db_path)
            folder.mkdir(parents=True, exist_ok=True)
            _private_folder(folder)
            aside = folder / f"damaged-{now:%Y%m%dT%H%M%SZ}.db"
            db_path.replace(aside)
            moved.append((db_path, aside))
            for suffix in suffixes:  # they belong to the file that was moved away
                leftover = Path(f"{db_path}{suffix}")
                if leftover.exists():
                    leftover.replace(Path(f"{aside}{suffix}"))
                    moved.append((leftover, Path(f"{aside}{suffix}")))
        installing = True
        fb._prepare(candidate, fb._page_size(candidate))
        _copy(candidate, db_path)
        _private_file(db_path)
    except BaseException:
        if installing:  # the files at the place are the new ones; before, they may still be the old ones
            fb.remove_database_files(db_path)
        try:
            for origin, where in reversed(moved):
                where.replace(origin)
        except OSError as error:
            raise RestoreNotUndone(aside or db_path) from error
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
    # Everything this command creates (the new database, its journal, folders) is private to the
    # service user, whatever the umask of the shell it runs from.
    previous_umask = os.umask(0o077) if os.name == "posix" else None
    state = {"touched": False}  # set when the database in place is about to be replaced
    lock = None
    try:
        # p91: one lock with the update, which systemd does not know of when it was typed by hand. No
        # data folder: nothing can run there.
        if db_path.parent.is_dir():
            try:
                lock = oplock.acquire(db_path.parent, "restore")
            except oplock.Held as held:
                complain(
                    "reprintbuddy: "
                    + (
                        "another restore is running."
                        if held.what == "restore"
                        else "an update is running. Wait until it has finished, then try again."
                    )
                )
                complain("Nothing was changed.")
                return EXIT_NOT_DONE
        return _restore(archive, db_path, runner, ask_password, say, complain, now, probe, state)
    except KeyboardInterrupt:
        complain("reprintbuddy: interrupted.")
        complain(
            "The database was being replaced: check it before you start Reprintbuddy."
            if state["touched"]
            else "Nothing was changed."
        )
        return EXIT_NOT_DONE
    finally:
        if lock is not None:
            lock.release()
        if previous_umask is not None:
            os.umask(previous_umask)


def _restore(archive, db_path, runner, ask_password, say, complain, now, probe, state) -> int:
    def refuse(code: int, text: str) -> int:
        complain(f"reprintbuddy: {text}")
        complain("Nothing was changed.")
        return code

    try:
        with open(archive, "rb"):
            pass
    except FileNotFoundError:
        return refuse(
            EXIT_NOT_DONE,
            f"file not found: {archive}. The command runs from the folder /, so give the full path.",
        )
    except OSError as error:
        return refuse(
            EXIT_NOT_DONE,
            f"cannot read {archive} ({type(error).__name__}). The command runs as the service user "
            "from the folder /, so give the full path; that user must be able to read the file "
            "(for example /var/lib/reprintbuddy, see the README).",
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
    except KeyboardInterrupt:  # Ctrl-C while checking: the decrypted copy must not stay
        fb.remove_database_files(candidate)
        raise

    try:
        # The password was typed and the database checked meanwhile: ask again whether anybody
        # started the service, right before the one step that replaces the database.
        why = probe() if probe is not None else service_running(db_path, runner)
        if why:
            return refuse(
                EXIT_NOT_DONE,
                f"Reprintbuddy was started meanwhile ({why}). Stop it first: systemctl stop reprintbuddy.",
            )
        state["touched"] = True
        readable = _readable(db_path)
        marks = current_marks(db_path) if readable else None  # p186: carried over to the next start
        notify_marks = current_notify_marks(db_path) if readable else None  # p220: likewise
        if readable:
            before = fb.replace_database(candidate, db_path, now=now)
        else:
            before = _install_unreadable(candidate, db_path, now)
    except RestoreNotUndone as error:
        complain(
            "reprintbuddy: the restore failed and the old database could not be put back. "
            f"It is at {error.where}; move it back to {db_path} by hand."
        )
        return EXIT_REFUSED
    except Exception as error:  # noqa: BLE001 - before or in the one-step copy
        state["touched"] = False
        return refuse(EXIT_REFUSED, f"the restore failed ({type(error).__name__}).")
    finally:
        fb.remove_database_files(candidate)  # whatever happened, the decrypted copy does not stay

    # The next start logs the restore into the restored database (Spec 10.7, group 4).
    write_restored_marker(
        db_path.parent,
        created_at=manifest.created_at,
        version=manifest.app_version,
        via="cli",
        marks=marks,
        notify_marks=notify_marks,
    )
    say(f"Restored the backup {_say_when(manifest)}.")
    if before is not None:
        say(f"The database that was there was saved as {before}.")
    else:
        say("There was no database to save.")
    say("Everybody has to sign in again, with the account of the backup.")
    say("Now start Reprintbuddy: systemctl start reprintbuddy")
    return EXIT_OK
