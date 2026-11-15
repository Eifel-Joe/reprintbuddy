"""The update helper: back up, put the package of the newest release in place, roll back on any failure.

It runs as its own process (`reprintbuddy update`, started by the systemd unit
`reprintbuddy-update.service`), never inside the web service: the service is
restarted midway and would take a child process down with it.

All imports are at the top of this module on purpose. pip replaces the package files
while the helper runs, and the rollback must not load any new code.

v0.4.9 p500 (Spec 11.3a "Paket je Release"): the new version is the package of a GitHub release, read
from the release list, downloaded, checked against its checksum (and GitHub's own) and unpacked into
`releases/<tag>` before anything of the running installation is touched; pip installs from there. The
rollback needs no git: the copy of the Python environment comes back without network, and
`release.json` names the new version only after a success. An installation made with git (up to
v0.4.9) takes this way too on its next update; its git copy goes after the first success.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import ssl
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from reprintbuddy.ops.backup import (
    KEEP_BACKUPS,
    backup_database,
    backup_dir_for,
    kept_for_running,
    prune_backups,
    restore_database,
)
from reprintbuddy.ops.migrationsteps import StepsError, graph_of_dir, single_head
from reprintbuddy.ops.installation import MARKER, RELEASES_DIR, Installation, read_installation, write_marker
from reprintbuddy.ops.oplock import LOCK_NAME, Held, acquire
from reprintbuddy.ops.package import (
    Download,
    PackageError,
    expected_sha256,
    prune_releases,
    remove_git_copy,
    unpack,
    verify,
)
from reprintbuddy.ops.releasenotes import HttpGet
from reprintbuddy.ops.releases import (
    CHANNELS,
    Channel,
    ReleaseCheckError,
    checksum_name,
    fetch_releases,
    newest_offered,
    package_name,
    return_target,
)
from reprintbuddy.ops.runner import CommandResult, CommandRunner
from reprintbuddy.ops.service import RESTART_WAIT_CMD, START_CMD, STOP_CMD, shutting_down
from reprintbuddy.ops.stablereturn import (
    RETURN_DIR,
    STARTED_MARK,
    ReturnPlan,
    assess,
    build_stable_db,
    copy_database,
)
from reprintbuddy.ops.status import UpdateStatus, is_busy, mark_interrupted, read_status, write_status
from reprintbuddy.ops.versions import is_newer, is_prerelease_tag

MAX_REASON = 300
DOWNLOAD_DIR = ".download"  # under releases/: the package and its checksum while they are checked
# p331 (v0.4.5): how old a "queued" may be for its target version to count. The button writes "queued" right
# before it starts the unit (sudo up to 15 s, `QUEUE_GRACE`), and the helper reads it as its first act; the
# window leaves room for a slow start. Root: the version was taken however old "queued" was, so a start left
# behind (cut off, p123) chose the version of a `reprintbuddy update` typed days later - an old one, or one
# deleted meanwhile ("unknown target"). Older than this, the update goes to the newest release, as typed by
# hand it should. Rejected: `QUEUE_GRACE` itself (a sudo near its limit plus the start of the program would
# miss it, and the update would then install something newer than the page offered); telling the unit from a
# hand by systemd's environment (a shell may carry it too). Tests: test_updater.py "p331".
QUEUED_TARGET_WINDOW = timedelta(minutes=2)
# Spec 11.3a "Rückweg zur stabilen Version": beside the database while the way back runs, the database as it
# was when the service stopped (`before.db`, what a failure brings back) and the one the stable version will
# run on (`stable.db`), in `RETURN_DIR` (from `stablereturn`). Gone after the way back, and in a rollback as soon
# as `before.db` is back in place (p610); only when it could not be put back does it stay and is named.


def venv_python(install_dir: Path) -> Path:
    return install_dir / ".venv" / "bin" / "python"


_NOISE_LINES = ("[SQL:", "[parameters:", "(Background on this error")
_HEADLINE = re.compile(r"^(fatal|error)\b", re.IGNORECASE)


_WRONG_PYTHON = re.compile(r"requires a different Python: (?P<found>[0-9][0-9.]*) not in '>=\s*(?P<needed>[0-9][0-9.]*)'")
# (pip names the version spec of the package; only the plain ">=3.x" form is turned into words)


def _too_old_python(result: CommandResult) -> tuple[str, dict[str, str]] | None:
    """pip's refusal because the environment's Python is older than the new version needs, in plain words.

    Root: a release has a Python floor (3.12 from v0.3.9 on, 3.13 from v0.3.12 on), and pip's own line
    ("requires a different Python: 3.12.3 not in '>=3.13'") says it only to those who know what it means.
    The update is rolled back as for any other install failure; only the text differs.

    Limit, stated plainly: the update runs the code of the INSTALLED version. An installation made on Debian 12
    (v0.3.8, Python 3.11) therefore updates with its old updater and shows pip's raw line; this text appears
    only from v0.3.9 on, for a later release that raises the floor again (its environment is then on the
    floor of the installed version or newer already; the Debian 13 installations run 3.13, so v0.3.12's floor
    does not stop them). The rollback itself is the one every install failure gets."""
    found = _WRONG_PYTHON.search("\n".join((result.stderr or "", result.stdout or "")))
    if found is None:
        return None
    text = (
        f"the new version needs Python {found['needed']} or newer, this installation runs Python {found['found']}; "
        "upgrade the system to a Debian release that has it, then run the install command again (it keeps "
        "your data), or set Reprintbuddy up again there and restore a backup"
    )
    return text, {"needed": found["needed"], "found": found["found"]}


def _reason(result: CommandResult, fallback: str) -> str:
    """The line that says what went wrong, short enough for a page.

    Not the first line: git starts with "From https://...", pip with retry warnings,
    Python with "Traceback". The last line that starts with fatal/error is meant; without
    one, the last line. SQLAlchemy appends the statement and its bound parameters (which
    can be setting values, even secrets): those never reach a page.
    """
    for text in (result.stderr, result.stdout):
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        lines = [line for line in lines if not line.startswith(_NOISE_LINES)]
        if not lines:
            continue
        line = ([line for line in lines if _HEADLINE.match(line)] or lines)[-1]
        line = line.split("[SQL:")[0].split("[parameters:")[0].strip()
        if line:
            return line[:MAX_REASON]
    return fallback or f"exit code {result.returncode}"


def _fresh_queued(previous: UpdateStatus, now: datetime) -> bool:
    """A start from the page that this run is picking up (p331): "queued", written within the window."""
    return (
        previous.state == "queued"
        and previous.started_at is not None
        and now - previous.started_at <= QUEUED_TARGET_WINDOW
    )


def run_update(
    db_path: Path,
    *,
    install_dir: Path,
    runner: CommandRunner,
    wait_healthy: Callable[[], bool],
    now: Callable[[], datetime],
    http_get: HttpGet,
    download: Download,
    target: str | None = None,
    keep_backups: int = KEEP_BACKUPS,
    script_location: Path | None = None,
    guard: InterruptGuard | None = None,
) -> UpdateStatus:
    """`script_location` (migration steps of the way back) exists for tests. `guard` is the one the SIGTERM
    handler uses (p610); without one a run cannot be cut off."""
    data_dir = db_path.parent
    started = now()
    guard = guard if guard is not None else InterruptGuard(db_path, now=now)

    previous = read_status(data_dir)
    # p91: one lock with the restore, also for an update typed by hand on the command line.
    # Refused: the status file stays as it is; the result says "running" so the command exits 3, not 0
    # (review C 3: after a successful update the old status would have made a refusal look like one).
    # p311: the lock is asked first, and it alone says whether an update runs. Root cause: the state file
    # was read first, and after a crash (a kill, a power cut) it said "running" for 30 minutes
    # (`STALE_AFTER`), so every new update was refused that long. The kernel frees the lock with its
    # process, so a free lock proves that the "running" in the file is a leftover. Limit: an update helper
    # older than v0.3.16 runs without the lock; an update typed by hand from the moment such a helper has
    # installed this version until it ends does not see it (only on a jump from v0.3.15 or older). Rejected: asking systemd as the page does - the
    # helper started by the unit is that unit, "active" would refuse every update the page starts.
    try:
        lock = acquire(data_dir, "update", tries=10)
    except Held as held:
        running = "a restore is running" if held.what == "restore" else "another update is running"
        print(f"update: {running}, nothing done", flush=True)
        return replace(previous, state="running")
    except OSError as error:
        print(f"update: cannot take the lock {data_dir / LOCK_NAME} ({error}), nothing done", flush=True)
        return replace(previous, state="running")
    if previous.state == "running" and is_busy(previous, started):
        print("update: an earlier update did not finish (its process ended); starting anew", flush=True)
    try:
        if target is None and previous.state == "queued" and previous.kind == "return":
            return _start_return(
                db_path, install_dir, runner, wait_healthy, now, http_get, download, keep_backups, previous, started,
                script_location, guard,
            )
        return _run_update(
            db_path, install_dir, runner, wait_healthy, now, http_get, download, target, keep_backups, previous, started,
            guard,
        )
    finally:
        lock.release()


def _read_channel(db_path: Path) -> Channel:
    """The choice "stable only" / "pre-releases too" (Spec 11.3a), from the database (plain sqlite3).
    Anything unreadable counts as the default, "stable"."""
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute("SELECT value FROM settings WHERE key = 'update_channel'").fetchone()
        finally:
            connection.close()
        value = json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return "stable"
    return value if value in CHANNELS else "stable"


def _run_update(
    db_path: Path,
    install_dir: Path,
    runner: CommandRunner,
    wait_healthy: Callable[[], bool],
    now: Callable[[], datetime],
    http_get: HttpGet,
    download: Download,
    target: str | None,
    keep_backups: int,
    previous: UpdateStatus,
    started: datetime,
    guard: InterruptGuard,
) -> UpdateStatus:
    data_dir = db_path.parent
    backup_dir = backup_dir_for(db_path)

    py = str(venv_python(install_dir))
    venv_dir = install_dir / ".venv"
    snapshot_dir = install_dir / ".venv.rollback"
    releases_dir = install_dir / RELEASES_DIR
    work_dir = releases_dir / DOWNLOAD_DIR
    snapshot_taken: list[bool] = []  # only a snapshot of THIS run may be restored, never a leftover
    info: dict[str, str | None] = {
        "from_tag": None,
        "to_tag": target or (previous.to_tag if _fresh_queued(previous, started) else None),
        "backup": None,
    }

    put, finish = _reporter(data_dir, backup_dir, keep_backups, info, started, now, guard=guard)
    new_source: Path | None = None

    def discard_download() -> None:
        shutil.rmtree(work_dir, ignore_errors=True)

    # --- before anything is changed: a failure leaves everything as it was -------------
    # v0.4.9 p500: what is installed comes from `release.json` (a package installation) or, for an
    # installation made with git, from git; where the new version comes from is the release list.
    # p610: a SIGTERM here changes nothing either; what was downloaded or unpacked goes.
    try:
        guard.arm()
        put("running", "backup")
        installed = read_installation(install_dir, runner)
        if installed is None or installed.repo is None:
            line = "no installation found" if installed is None else "the repository of the installation is unknown"
            return finish("failed", "backup", "backup: " + line, "head_unreadable", {"detail": line})
        info["from_tag"] = installed.tag
        try:
            backup = backup_database(db_path, backup_dir, label=installed.label, now=now())
        except (OSError, sqlite3.Error) as error:
            detail = str(error)[:MAX_REASON]
            return finish("failed", "backup", f"backup: {detail}", "backup_failed", {"detail": detail})
        info["backup"] = str(backup)

        put("running", "fetch")
        try:
            releases = fetch_releases(installed.repo, http_get)
        except ReleaseCheckError as error:
            detail = str(error)[:MAX_REASON]
            return finish("failed", "fetch", f"fetch: {detail}", "fetch_failed", {"detail": detail})
        # The version the page offered (or the one asked for) is installed, not whatever is newest by now;
        # if it is gone, the update stops instead of installing something else. Without one, the newest the
        # chosen channel offers (Spec 11.3a "Vorabversionen").
        wanted = target or info["to_tag"]
        chosen = next((item for item in releases if item.tag == wanted), None) if wanted else newest_offered(
            releases, _read_channel(db_path)
        )
        if chosen is None and not wanted:
            return finish("failed", "fetch", "fetch: no newer release", "no_newer")
        if chosen is None:
            return finish("failed", "fetch", f"fetch: unknown target {wanted}", "unknown_target", {"detail": str(wanted)})
        goal = chosen.tag
        info["to_tag"] = goal
        if not is_newer(goal, info["from_tag"]):
            return finish("failed", "fetch", "fetch: no newer release", "no_newer")

        # Download and check the package, then unpack it beside the installed version. Nothing of the running
        # installation is touched yet: a failure here removes what was downloaded and changes nothing else.
        package = work_dir / package_name(goal)
        checksum_file = work_dir / checksum_name(goal)
        new_source = releases_dir / goal

        try:
            shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True)
            download(chosen.package_url, package)
            download(chosen.checksum_url, checksum_file)
            expected = expected_sha256(checksum_file.read_text(encoding="utf-8", errors="replace"), package_name(goal))
            sha256 = verify(package, expected, chosen.digest)
        except (PackageError, OSError) as error:
            discard_download()
            reason = error.reason if isinstance(error, PackageError) else "fetch_failed"
            detail = (error.detail or error.reason) if isinstance(error, PackageError) else type(error).__name__
            return finish("failed", "fetch", f"fetch: {detail}"[:MAX_REASON], reason, {"detail": detail[:MAX_REASON]})

        put("running", "checkout")
        try:
            unpack(package, releases_dir, goal)
        except PackageError as error:
            discard_download()
            return finish("failed", "checkout", f"checkout: {error.detail or error.reason}", error.reason, {"detail": error.detail})
        discard_download()
        guard.hold()
    except Interrupted:
        discard_download()
        if new_source is not None:
            shutil.rmtree(new_source, ignore_errors=True)
            # `package.unpack` cleans its folder after an Exception only (review of p610)
            shutil.rmtree(new_source.with_name(f".unpack-{new_source.name}"), ignore_errors=True)
        step = str(info.get("step") or "backup")
        return finish("failed", step, f"{step}: interrupted, nothing was changed", "interrupted_untouched")

    # --- from pip on, any failure is rolled back ---------------------------------------
    current = "install"
    pip_ran: list[bool] = []  # p610: a signal may come before pip; then the environment is untouched

    def forward() -> tuple[str, str, str, dict[str, str]] | None:
        """None when everything went through, else (step, English line, reason code, parameters)."""
        nonlocal current
        current = "install"
        put("running", "install")
        problem = _install_package(py, venv_dir, snapshot_dir, new_source, runner, snapshot_taken, pip_ran)
        if problem is not None:
            return problem

        current = "verify"
        put("running", "verify")
        problem = _verify_version(py, goal, runner)
        if problem is not None:
            return problem

        current = "migrate"
        put("running", "migrate")
        done = runner((py, "-m", "reprintbuddy", "migrate", "--db", str(db_path)), timeout=300.0)
        if not done.ok:
            return "migrate", _reason(done, "the database migration failed"), "migrate_failed", {"detail": _reason(done, "")}

        current = "restart"
        put("running", "restart")
        done = runner(RESTART_WAIT_CMD, timeout=120.0)
        if not done.ok:
            return "restart", _reason(done, "the restart failed"), "restart_failed", {"detail": _reason(done, "")}

        current = "health"
        put("running", "health")
        if not wait_healthy():
            return "health", "the new version did not answer after the restart", "health_failed", {}
        return None

    def backward() -> tuple[bool, bool]:
        """Previous environment, previous database, restart. Returns whether everything is back in place and
        whether the old version answers.

        p610: runs to its end whatever signal comes (the guard holds); after one, each command is short. A signal
        that came before pip ran (while the environment was copied) leaves the environment as it is: a reinstall,
        cut at 20 s, could break one that was never touched (review)."""
        ok = True
        if not snapshot_taken:
            shutil.rmtree(snapshot_dir, ignore_errors=True)  # half a copy, if any
        if snapshot_taken and snapshot_dir.is_dir():
            # The environment as it was before pip: no network, no reinstall needed.
            try:
                shutil.rmtree(venv_dir, ignore_errors=True)
                snapshot_dir.rename(venv_dir)
            except OSError:
                ok = False
            done = CommandResult(0)
        elif not pip_ran:
            done = CommandResult(0)
        else:
            shutil.rmtree(installed.source_dir / "build", ignore_errors=True)
            done = runner(
                (py, "-m", "pip", "install", "--no-cache-dir", "--upgrade", str(installed.source_dir)),
                timeout=guard.timeout(900.0),
            )
        if not done.ok:
            # No network for pip is the usual reason the update failed, so the reinstall of
            # the old version fails too. What matters is whether the old version is the one
            # installed and answering, not pip's exit code.
            old = info["from_tag"]
            shown = _service_command(runner, guard, (py, "-m", "reprintbuddy", "--version"), 120.0)
            ok &= bool(old) and shown.ok and shown.stdout.strip() == "reprintbuddy " + str(old)[1:]
        # The new version's folder goes; `release.json` was never touched, so it still names the old one.
        shutil.rmtree(new_source, ignore_errors=True)
        # Restore the database while nothing has it open. If the service cannot be stopped it
        # may still run the new code: then a restart (below) is the way back.
        stopped = _service_command(runner, guard, STOP_CMD, 120.0)
        try:
            restore_database(backup, db_path)
        except (OSError, sqlite3.Error):
            ok = False
        done = _service_command(runner, guard, START_CMD if stopped.ok else RESTART_WAIT_CMD, 120.0)
        return ok, bool(wait_healthy()) and done.ok

    problem, cut_off = _armed(guard, forward, lambda: current)
    if problem is None:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        _settle(install_dir, installed, goal, sha256, db_path)
        return finish("succeeded", "done", "")

    cause = f"{problem[0]}: {problem[1]}"
    put("running", "rollback", cause, reason=problem[2], params=problem[3])
    try:
        put_back, answers = backward()
    except Exception:  # noqa: BLE001
        put_back = answers = False
    restored = put_back and answers
    if cut_off:
        # p610 (Spec 11.3a): stopped from outside counts as a failure; the log says "interrupted" (": interrupted").
        line = _cut_off_line(runner, put_back, answers)
        if line is not None:
            return finish("rolled_back", "rollback", f"{problem[0]}: {line}", "interrupted_rolled_back")
        cause = f"{problem[0]}: {CUT_OFF_PARTIAL}"
        return finish("rollback_failed", "rollback", f"{cause} \u2013 backup: {backup}", "interrupted_partial")
    if restored:
        return finish("rolled_back", "rollback", cause, problem[2], problem[3])
    return finish("rollback_failed", "rollback", f"{cause} – backup: {backup}", problem[2], problem[3])


def _reporter(
    data_dir: Path,
    backup_dir: Path,
    keep_backups: int,
    info: dict[str, str | None],
    started: datetime,
    now: Callable[[], datetime],
    guard: InterruptGuard | None = None,
    **extra: object,
) -> tuple[Callable[..., UpdateStatus], Callable[..., UpdateStatus]]:
    """`put` writes a state of the run, `finish` its final one (and prunes the backups). `extra` are the fields
    of the way back (`kind`, `return_case`, ...), the same in every state of the run. `info["step"]` is the last
    step written (p610: the step a SIGTERM cut off); from `finish` on a SIGTERM cuts nothing off."""

    def put(
        state: str, step: str, message: str = "", *, finished: bool = False,
        reason: str = "", params: dict[str, str] | None = None,
    ) -> UpdateStatus:
        status = UpdateStatus(
            state=state,  # type: ignore[arg-type]
            step=step,
            from_tag=info["from_tag"],
            to_tag=info["to_tag"],
            started_at=started,
            finished_at=now() if finished else None,
            message=message,
            backup=info["backup"],
            reason=reason,
            params=params or {},
            **extra,  # type: ignore[arg-type]
        )
        info["step"] = step
        try:
            write_status(data_dir, status)
        except OSError as error:
            # A full disk or a read-only file must not stop an update half way, least of all
            # the rollback: the page just keeps the last state it could get.
            print(f"update: cannot write the status file ({error})", flush=True)
        print(f"update: {step}: {state}" + (f" ({message})" if message else ""), flush=True)
        return status

    def finish(
        state: str, step: str, message: str, reason: str = "", params: dict[str, str] | None = None
    ) -> UpdateStatus:
        if guard is not None:
            guard.hold()
        if info["backup"]:
            # After a failed rollback the message names this backup as the one to restore.
            protect = [Path(info["backup"])] if state == "rollback_failed" else []
            # Spec 11.3a "Sicherung von vor der Vorabversion": while a pre-release runs afterwards, the backup
            # from before the switch to it stays beyond the usual five (the way back may need it).
            running = info["to_tag"] if state == "succeeded" else info["from_tag"]
            protect += kept_for_running(backup_dir, running)
            prune_backups(backup_dir, keep_backups, protect=protect)
        return put(state, step, message, finished=True, reason=reason, params=params)

    return put, finish


Problem = tuple[str, str, str, dict[str, str]]  # (step, English line, reason code, parameters)

# p610: the English lines of a run stopped from outside (after "<step>: "); ": interrupted" makes the log say
# "interrupted" (`activity.states.INTERRUPTED`).
CUT_OFF_ROLLED_BACK = "interrupted, the previous version was put back"
CUT_OFF_PARTIAL = "interrupted, the installation may be incomplete"
CUT_OFF_AT_SHUTDOWN = "; it starts with the next boot"


def _cut_off_line(runner: CommandRunner, put_back: bool, answers: bool) -> str | None:
    """The line of a rollback after a stop from outside that counts as done, or None (it failed).

    Done when everything is back in place and the previous version answers, or when it is back in place and the
    system shuts down (review of p610): then the service is not running now, but the unit is enabled and starts
    the previous version with the next boot (Spec 11.3a). Asked only then: systemd's answer to a start during a
    shutdown is not documented, its state "stopping" is."""
    if put_back and answers:
        return CUT_OFF_ROLLED_BACK
    if put_back and shutting_down(runner):
        return CUT_OFF_ROLLED_BACK + CUT_OFF_AT_SHUTDOWN
    return None


def _armed(
    guard: InterruptGuard, forward: Callable[[], Problem | None], current: Callable[[], str]
) -> tuple[Problem | None, bool]:
    """Run `forward` so that a SIGTERM cuts it off (p610). Returns its problem (None: it went through) and
    whether a SIGTERM cut it off. Afterwards the guard holds: the rollback and the final state are not cut off.

    The nesting matters: a signal in the `except Exception` branch, or right after `forward` returned, is still
    caught by the outer `except Interrupted`; `guard.hold()` is the last thing inside it."""
    try:
        try:
            guard.arm()
            problem = forward()
        except Exception as error:  # noqa: BLE001 - whatever happened, the rollback must still run
            step = current()
            problem = (step, f"unexpected error: {type(error).__name__}", "unexpected", {"detail": type(error).__name__})
        guard.hold()
        return problem, False
    except Interrupted:
        return (current(), "interrupted", "interrupted_partial", {}), True


def _service_command(
    runner: CommandRunner, guard: InterruptGuard, args: tuple[str, ...], timeout: float
) -> CommandResult:
    """A command of the rollback. p610: short once a SIGTERM came, and run once more if a SIGTERM came while it
    ran and it failed: systemd sends SIGTERM to every process of the unit, also to a `systemctl` running then.
    Starting or stopping twice is harmless."""
    signals = guard.signals
    done = runner(args, timeout=guard.timeout(timeout))
    if not done.ok and guard.signals > signals:
        done = runner(args, timeout=guard.timeout(timeout))
    return done


def _install_package(
    py: str,
    venv_dir: Path,
    snapshot_dir: Path,
    new_source: Path,
    runner: CommandRunner,
    snapshot_taken: list[bool],
    pip_ran: list[bool],
) -> Problem | None:
    """pip installs the unpacked version; a copy of the environment comes first (the way back to it). `pip_ran`
    is set right before pip runs (p610: before it, the environment is untouched)."""
    # A pip that fails half way can leave the environment neither old nor new, and the
    # reinstall of the old version needs the network that just failed. Keep a copy.
    if venv_dir.is_dir():
        try:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            shutil.copytree(venv_dir, snapshot_dir, symlinks=True)
            snapshot_taken.append(True)
        except OSError as error:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            detail = str(error)[:200]
            return "install", f"cannot back up the Python environment: {detail}", "snapshot_failed", {"detail": detail}
    shutil.rmtree(new_source / "build", ignore_errors=True)
    pip_ran.append(True)
    done = runner((py, "-m", "pip", "install", "--no-cache-dir", "--upgrade", str(new_source)), timeout=900.0)
    if not done.ok:
        too_old = _too_old_python(done)
        if too_old is not None:
            return "install", too_old[0], "python_too_old", too_old[1]
        return "install", _reason(done, "pip install failed"), "install_failed", {"detail": _reason(done, "")}
    return None


def _verify_version(py: str, goal: str, runner: CommandRunner) -> Problem | None:
    done = runner((py, "-m", "reprintbuddy", "--version"))
    expected = "reprintbuddy " + goal[1:]
    if not done.ok or done.stdout.strip() != expected:
        seen = done.stdout.strip()[:80] or _reason(done, "no output")
        return "verify", f"expected '{expected}', got '{seen}'", "verify_failed", {"expected": expected, "seen": seen}
    return None


# --- the way back to the stable version (Spec 11.3a "Rückweg zur stabilen Version") -----------------


def _start_return(
    db_path: Path,
    install_dir: Path,
    runner: CommandRunner,
    wait_healthy: Callable[[], bool],
    now: Callable[[], datetime],
    http_get: HttpGet,
    download: Download,
    keep_backups: int,
    previous: UpdateStatus,
    started: datetime,
    script_location: Path | None,
    guard: InterruptGuard,
) -> UpdateStatus:
    """A request for the way back from the page. One that grew old changes nothing: a click on "back" must never
    lead forward, as an ordinary update to a newer pre-release would. The state it leaves makes the next run by
    hand an ordinary update again."""
    if not _fresh_queued(previous, started):
        info: dict[str, str | None] = {"from_tag": previous.from_tag, "to_tag": previous.to_tag, "backup": None}
        _, finish = _reporter(
            db_path.parent, backup_dir_for(db_path), keep_backups, info, started, now,
            kind="return", return_case=previous.return_case, target_head=previous.target_head,
            return_backup=previous.return_backup,
        )
        return finish("failed", "start", "start: the request for the way back is too old", "return_expired")
    return _run_return(
        db_path, install_dir, runner, wait_healthy, now, http_get, download, keep_backups, previous, started,
        script_location, guard,
    )


def _plan_changed(plan: ReturnPlan, previous: UpdateStatus) -> str | None:
    """What differs from the case the user confirmed; None when it is the same."""
    if plan.case is None:
        return f"no way back any more ({plan.unavailable})"
    if plan.case != previous.return_case:
        return f"case {plan.case} instead of the confirmed case {previous.return_case}"
    if plan.case == 3:
        confirmed = Path(previous.return_backup).name if previous.return_backup else None
        if plan.backup is None or plan.backup.name != confirmed:
            return f"the backup is now {plan.backup.name if plan.backup else 'missing'}, not {confirmed}"
    return None


def _log_losses(plan: ReturnPlan) -> None:
    """What the way back removes, counted now (it may have grown since the confirmation page)."""
    if plan.case == 2:
        for loss in plan.losses:
            print(f"update: way back: {loss.note['en']}: {loss.count} entries go", flush=True)
        if not plan.losses:
            print("update: way back: the steps of the pre-release hold no entries; nothing is lost", flush=True)
    elif plan.case == 3:
        print(
            f"update: way back: the backup {plan.backup} comes back; {len(plan.journal_ids)} journal entries and "
            f"{plan.activity_since} log entries made since then are lost",
            flush=True,
        )


def _run_return(
    db_path: Path,
    install_dir: Path,
    runner: CommandRunner,
    wait_healthy: Callable[[], bool],
    now: Callable[[], datetime],
    http_get: HttpGet,
    download: Download,
    keep_backups: int,
    previous: UpdateStatus,
    started: datetime,
    script_location: Path | None,
    guard: InterruptGuard,
) -> UpdateStatus:
    """The way back, in the order of Spec 11.3a: backup of the current state, the package of the stable version
    loaded and checked; then the service stops, the database, the package, the start, the check.

    The live database changes once, in one SQLite transaction (`swap`, the backup API), before pip: all Alembic
    work (the case, the downgrade of case 2) lies before pip, which replaces the package files of this code.
    Before `swap` the live database is untouched, so a failure there only starts the pre-release again; from
    `swap` on, a failure brings back `before.db`, the database as it was when the service stopped, and the
    environment of the pre-release. The case is decided again here (`stablereturn.assess`); if it is not the
    one the user confirmed, nothing is changed. Tests: tests/ops/test_updater_return.py."""
    data_dir = db_path.parent
    backup_dir = backup_dir_for(db_path)
    py = str(venv_python(install_dir))
    venv_dir = install_dir / ".venv"
    snapshot_dir = install_dir / ".venv.rollback"
    releases_dir = install_dir / RELEASES_DIR
    work_dir = releases_dir / DOWNLOAD_DIR
    return_dir = data_dir / RETURN_DIR
    before_db = return_dir / "before.db"
    snapshot_taken: list[bool] = []
    info: dict[str, str | None] = {"from_tag": previous.from_tag, "to_tag": previous.to_tag, "backup": None}
    put, finish = _reporter(
        data_dir, backup_dir, keep_backups, info, started, now, guard,
        kind="return", return_case=previous.return_case, target_head=previous.target_head,
        return_backup=previous.return_backup,
    )
    new_source: Path | None = None

    # --- before the service stops: a failure changes nothing ------------------------------------------
    if before_db.exists():
        # Left by an earlier way back whose rollback failed (its message names this file as the database as it
        # was) or that was cut off: it may be the only copy of those entries, so it is never deleted here.
        line = f"a database kept by an earlier way back lies at {before_db}; restore or move it first"
        return finish("failed", "start", "start: " + line, "return_unavailable", {"detail": line})
    # p610: a SIGTERM here changes nothing either; what was downloaded or unpacked goes.
    try:
        guard.arm()
        put("running", "backup")
        installed = read_installation(install_dir, runner)
        if installed is None or installed.repo is None:
            line = "no installation found" if installed is None else "the repository of the installation is unknown"
            return finish("failed", "backup", "backup: " + line, "head_unreadable", {"detail": line})
        info["from_tag"] = installed.tag
        if not installed.tag or not is_prerelease_tag(installed.tag):
            line = "no pre-release is installed"
            return finish("failed", "backup", "backup: " + line, "return_unavailable", {"detail": line})
        try:
            backup = backup_database(db_path, backup_dir, label=installed.label, now=now())
        except (OSError, sqlite3.Error) as error:
            detail = str(error)[:MAX_REASON]
            return finish("failed", "backup", f"backup: {detail}", "backup_failed", {"detail": detail})
        info["backup"] = str(backup)

        put("running", "fetch")
        try:
            releases = fetch_releases(installed.repo, http_get)
        except ReleaseCheckError as error:
            detail = str(error)[:MAX_REASON]
            return finish("failed", "fetch", f"fetch: {detail}", "fetch_failed", {"detail": detail})
        chosen = return_target(releases, installed.tag)
        if chosen is None or chosen.tag != previous.to_tag:
            detail = f"the stable version to go back to is now {chosen.tag if chosen else 'none'}, not {previous.to_tag}"
            return finish("failed", "fetch", f"fetch: {detail}", "return_changed", {"detail": detail})
        goal = chosen.tag
        package = work_dir / package_name(goal)
        checksum_file = work_dir / checksum_name(goal)
        new_source = releases_dir / goal
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True)
            download(chosen.package_url, package)
            download(chosen.checksum_url, checksum_file)
            expected = expected_sha256(checksum_file.read_text(encoding="utf-8", errors="replace"), package_name(goal))
            sha256 = verify(package, expected, chosen.digest)
        except (PackageError, OSError) as error:
            shutil.rmtree(work_dir, ignore_errors=True)
            reason = error.reason if isinstance(error, PackageError) else "fetch_failed"
            detail = (error.detail or error.reason) if isinstance(error, PackageError) else type(error).__name__
            return finish("failed", "fetch", f"fetch: {detail}"[:MAX_REASON], reason, {"detail": detail[:MAX_REASON]})

        put("running", "checkout")
        try:
            unpack(package, releases_dir, goal)
        except PackageError as error:
            shutil.rmtree(work_dir, ignore_errors=True)
            return finish("failed", "checkout", f"checkout: {error.detail or error.reason}", error.reason, {"detail": error.detail})
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            target_graph = graph_of_dir(new_source / "src" / "reprintbuddy" / "migrations" / "versions")
        except StepsError as error:
            shutil.rmtree(new_source, ignore_errors=True)
            return finish("failed", "checkout", f"checkout: {error}", "package_unusable", {"detail": str(error)})
        head = single_head(target_graph)
        if head != previous.target_head:
            shutil.rmtree(new_source, ignore_errors=True)
            detail = f"the steps of {goal} end at {head}, the check saw {previous.target_head}"
            return finish("failed", "checkout", f"checkout: {detail}", "return_changed", {"detail": detail})
        guard.hold()
    except Interrupted:
        shutil.rmtree(work_dir, ignore_errors=True)
        if new_source is not None:
            shutil.rmtree(new_source, ignore_errors=True)
            shutil.rmtree(new_source.with_name(f".unpack-{new_source.name}"), ignore_errors=True)
        step = str(info.get("step") or "backup")
        return finish("failed", step, f"{step}: interrupted, nothing was changed", "interrupted_untouched")

    # --- the service stops; until `swap` the live database is untouched ------------------------------
    def resume(step: str, message: str, reason: str, params: dict[str, str], cut_off: bool = False) -> UpdateStatus:
        """Nothing changed yet: the pre-release starts again on its own database. p610: not cut off by a signal,
        and short after one."""
        guard.hold()
        shutil.rmtree(new_source, ignore_errors=True)
        shutil.rmtree(return_dir, ignore_errors=True)
        if _service_command(runner, guard, START_CMD, 120.0).ok and wait_healthy():
            return finish("failed", step, message, reason, params)
        if cut_off and shutting_down(runner):
            # nothing was changed; the enabled unit starts the pre-release with the next boot
            return finish("failed", step, message + CUT_OFF_AT_SHUTDOWN, reason, params)
        return finish("rollback_failed", step, f"{message} – the pre-release did not answer after its start", reason, params)

    # p610: a SIGTERM while the service is stopped starts the pre-release again (it stayed stopped until the next
    # boot up to 1.2.0-beta.1); the live database is untouched until `swap`.
    try:
        guard.arm()
        put("running", "stop")
        stopped = runner(STOP_CMD, timeout=120.0)
        if not stopped.ok:
            guard.hold()
            shutil.rmtree(new_source, ignore_errors=True)
            detail = _reason(stopped, "the service could not be stopped")
            # whatever half of it happened, the pre-release runs on - if it answers (review: say so when not)
            if _service_command(runner, guard, START_CMD, 120.0).ok and wait_healthy():
                return finish("failed", "stop", f"stop: {detail}", "stop_failed", {"detail": detail})
            message = f"stop: {detail} – the pre-release did not answer after its start"
            return finish("rollback_failed", "stop", message, "stop_failed", {"detail": detail})

        put("running", "database")
        try:
            shutil.rmtree(return_dir, ignore_errors=True)  # no `before.db` in it: checked at the start
            return_dir.mkdir(parents=True)
            copy_database(db_path, before_db)
            print(f"update: way back: the database as it was is kept at {before_db} until the way back is through", flush=True)
            plan = assess(
                db_path, target=goal, target_graph=target_graph, backup_dir=backup_dir, work_dir=return_dir / "trial",
                script_location=script_location,
            )
        except Exception as error:  # noqa: BLE001 - nothing has changed: whatever it was, the pre-release runs on
            detail = f"{type(error).__name__}: {error}"[:MAX_REASON]
            return resume("database", f"database: {detail}", "database_failed", {"detail": detail})
        changed = _plan_changed(plan, previous)
        if changed is not None:
            return resume("database", f"database: {changed}", "return_changed", {"detail": changed})
        _log_losses(plan)
        try:
            stable_db = build_stable_db(plan, db_path, return_dir / "stable.db", script_location)
        except Exception as error:  # noqa: BLE001
            detail = f"{type(error).__name__}: {error}"[:MAX_REASON]
            return resume("database", f"database: {detail}", "database_failed", {"detail": detail})
        guard.hold()
    except Interrupted:
        step = str(info.get("step") or "stop")
        return resume(step, f"{step}: interrupted, nothing was changed", "interrupted_untouched", {}, cut_off=True)

    # --- from `swap` on, a failure brings back the database as it was and the pre-release -------------
    swapped: list[bool] = []
    pip_ran: list[bool] = []
    current = "swap"

    def forward() -> Problem | None:
        nonlocal current
        current = "swap"
        put("running", "swap")
        swapped.append(True)  # a failed swap is rolled back by SQLite; restoring `before.db` costs nothing
        try:
            restore_database(stable_db, db_path)
        except (OSError, sqlite3.Error) as error:
            detail = f"{type(error).__name__}: {error}"[:MAX_REASON]
            return "swap", detail, "swap_failed", {"detail": detail}
        current = "install"
        put("running", "install")
        problem = _install_package(py, venv_dir, snapshot_dir, new_source, runner, snapshot_taken, pip_ran)
        if problem is not None:
            return problem
        current = "verify"
        put("running", "verify")
        problem = _verify_version(py, goal, runner)
        if problem is not None:
            return problem
        current = "migrate"
        put("running", "migrate")
        done = runner((py, "-m", "reprintbuddy", "migrate", "--db", str(db_path)), timeout=300.0)
        if not done.ok:
            return "migrate", _reason(done, "the database migration failed"), "migrate_failed", {"detail": _reason(done, "")}
        current = "restart"
        put("running", "restart")
        done = runner(START_CMD, timeout=120.0)
        if not done.ok:
            return "restart", _reason(done, "the start failed"), "restart_failed", {"detail": _reason(done, "")}
        current = "health"
        put("running", "health")
        if not wait_healthy():
            return "health", "the stable version did not answer after its start", "health_failed", {}
        return None

    def backward() -> bool:
        """The environment and the database of the pre-release, its start. True only if it answers.

        p610: runs to its end whatever signal comes (the guard holds); after one, each command is short.
        `.return` goes as soon as the live database is the one from before the way back (or was never touched):
        left there, the start after a power cut would put `before.db` back a second time
        (`stablereturn.recover_cut_off_return`) and lose what the running pre-release took in meanwhile.
        Rejected: removing it only after the answer (as up to 1.2.0-beta.1); that leaves exactly this window."""
        ok = True
        if not snapshot_taken:
            shutil.rmtree(snapshot_dir, ignore_errors=True)  # half a copy, if any (p610)
        if snapshot_taken and snapshot_dir.is_dir():
            try:
                shutil.rmtree(venv_dir, ignore_errors=True)
                snapshot_dir.rename(venv_dir)
            except OSError:
                ok = False
        shutil.rmtree(new_source, ignore_errors=True)
        # the stable version may have started (restart, health)
        stopped = _service_command(runner, guard, STOP_CMD, 120.0)
        back_in_place = not swapped
        if swapped:
            try:
                restore_database(before_db, db_path)
                back_in_place = True
            except (OSError, sqlite3.Error):
                ok = False
        if back_in_place:
            shutil.rmtree(return_dir, ignore_errors=True)
        else:
            # The pre-release now starts on the switched database and takes in entries: a later start must not
            # put `before.db` back over them (review of p610, `stablereturn.recover_cut_off_return`).
            try:
                (return_dir / STARTED_MARK).write_text("", encoding="utf-8")
            except OSError:
                pass
        done = _service_command(runner, guard, START_CMD if stopped.ok else RESTART_WAIT_CMD, 120.0)
        return ok, bool(wait_healthy()) and done.ok

    problem, cut_off = _armed(guard, forward, lambda: current)
    if problem is None:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        _settle(install_dir, installed, goal, sha256, db_path)
        shutil.rmtree(return_dir, ignore_errors=True)
        return finish("succeeded", "done", "")

    cause = f"{problem[0]}: {problem[1]}"
    put("running", "rollback", cause, reason=problem[2], params=problem[3])
    try:
        put_back, answers = backward()
    except Exception:  # noqa: BLE001
        put_back = answers = False
    restored = put_back and answers
    if cut_off:
        # p610 (Spec 11.3a): stopped from outside counts as a failure; the log says "interrupted" (": interrupted").
        done_line = _cut_off_line(runner, put_back, answers)
        restored = done_line is not None
        line, code = (done_line, "interrupted_rolled_back") if done_line else (CUT_OFF_PARTIAL, "interrupted_partial")
        problem = (problem[0], line, code, {})
        cause = f"{problem[0]}: {line}"
    if restored:
        shutil.rmtree(return_dir, ignore_errors=True)
        return finish("rolled_back", "rollback", cause, problem[2], problem[3])
    kept = ""
    if before_db.is_file():
        print(f"update: way back: the database as it was before it stays at {before_db}", flush=True)
        kept = f" – database as it was: {before_db}"
    return finish("rollback_failed", "rollback", f"{cause}{kept} – backup: {backup}", problem[2], problem[3])


def _settle(install_dir: Path, installed: Installation, goal: str, sha256: str, db_path: Path) -> None:
    """After a successful update: `release.json` names the new version, the old git copy of an
    installation made with git goes (Spec 11.3a), and of the unpacked versions only the new and the
    previous one stay. Nothing here may turn the success into a failure: the new version runs."""
    if installed.repo is None:  # cannot happen: the update stops before without a repository
        return
    try:
        write_marker(install_dir, tag=goal, repo=installed.repo, sha256=sha256)
    except OSError as error:
        print(f"update: cannot write {MARKER} ({error}); the next update reads the version from git", flush=True)
        return
    if installed.kind == "git":
        removed, failed = remove_git_copy(install_dir, protect=[db_path])
        for name in removed:
            print(f"update: removed the old git copy: {install_dir / name}", flush=True)
        for name in failed:
            print(f"update: could not remove the old git copy: {install_dir / name}", flush=True)
    keep = {goal} | ({installed.tag} if installed.tag else set())
    for name in prune_releases(install_dir, keep=keep):
        print(f"update: removed the unpacked version {name}", flush=True)


class Interrupted(BaseException):
    """SIGTERM while a run is armed. Not an `Exception`: it passes every `except Exception` (and the runner, which
    kills its child on the way) up to the place that rolls back."""


# p610: what a rollback after SIGTERM may take per command. systemd sends SIGKILL `TimeoutStopSec` after SIGTERM,
# 90 s by default (systemd-system.conf "DefaultTimeoutStopSec="); stop, restore, start and a short wait for the
# answer (`HURRY_WAIT`) fit in.
HURRY_TIMEOUT = 20.0
HURRY_WAIT = 20.0


class InterruptGuard:
    """What SIGTERM does to the helper (p610, Spec 11.3a: stopped from outside counts as a failure).

    Root: up to 1.2.0-beta.1 the handler wrote the final state and ended the process with `SystemExit`. The
    rollback never ran (it catches `Exception` only), and on the way back the service it had stopped stayed
    stopped until the next boot. Now three states:
    - idle (before the lock, outside a run): as before, the final state is written and the process ends;
    - armed (the steps that may be cut off): `Interrupted` is raised and the run rolls back;
    - holding (rollback, final state): the signal is noted (`pending`) and nothing is cut off.
    The first signal in "armed" switches to "holding": systemd sends SIGTERM to every process of the unit, and a
    second one must not cut off the rollback. Rejected: a handler that only sets a flag and lets each step look
    at it; pip, a download or a copy of the database would run on until they end, beyond the 90 s to SIGKILL.
    Tests: tests/ops/test_updater_interrupt.py."""

    def __init__(self, db_path: Path, *, now: Callable[[], datetime]) -> None:
        self._db_path = db_path
        self._now = now
        self.state = "idle"
        self.signals = 0

    @property
    def pending(self) -> bool:
        return self.signals > 0

    def signal(self, signum: int = 15) -> None:
        """What the SIGTERM handler does."""
        self.signals += 1
        if self.state == "armed":
            self.state = "holding"
            raise Interrupted(signum)
        if self.state == "holding":
            return
        try:
            mark_interrupted(self._db_path.parent, self._now())
        except OSError:
            pass  # nothing more can be done about a status file that cannot be written
        raise SystemExit(128 + signum)

    def arm(self) -> None:
        """From here a signal cuts the run off; one that came while holding does so at once."""
        if self.pending:
            self.state = "holding"
            raise Interrupted(15)
        self.state = "armed"

    def hold(self) -> None:
        self.state = "holding"

    def timeout(self, seconds: float) -> float:
        """The time a command may take: short once a signal came."""
        return min(seconds, HURRY_TIMEOUT) if self.pending else seconds


def interrupt_handler(
    db_path: Path, *, now: Callable[[], datetime], guard: InterruptGuard | None = None
) -> Callable[[int, object], None]:
    """A SIGTERM handler for the helper: systemd stops a unit that runs past its timeout, that is stopped, or
    on shutdown. It hands the signal to `guard` (see `InterruptGuard`); without one it writes the final state
    and exits, as up to 1.2.0-beta.1."""
    target = guard if guard is not None else InterruptGuard(db_path, now=now)

    def handle(signum: int, frame: object) -> None:
        target.signal(signum)

    return handle


def _read_address(db_path: Path) -> str:
    """The address the service listens on, from the database (plain sqlite3, no packages)."""
    if not db_path.is_file():
        raise FileNotFoundError(str(db_path))  # sqlite3.connect would create an empty database
    connection = sqlite3.connect(str(db_path), timeout=5)
    try:
        rows = dict(
            connection.execute(
                "SELECT key, value FROM settings WHERE key IN ('host', 'port', 'tls_cert', 'tls_key')"
            ).fetchall()
        )
    finally:
        connection.close()
    saved = {key: json.loads(value) for key, value in rows.items()}
    host = saved.get("host") or "0.0.0.0"
    port = int(saved.get("port") or 8080)
    tls = bool(saved.get("tls_cert") and saved.get("tls_key"))
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"  # a wildcard means "this machine"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{'https' if tls else 'http'}://{host}:{port}"


def http_health_waiter(
    db_path: Path,
    *,
    timeout_s: float = 90.0,
    interval_s: float = 2.0,
    hurry: Callable[[], bool] | None = None,
    hurry_s: float = HURRY_WAIT,
) -> Callable[[], bool]:
    """A function that waits until the service answers `/healthz`, or gives up.

    Only the standard library is used (no SQLAlchemy, no httpx): pip replaces the
    installed packages while the helper runs, and a module imported lazily afterwards
    could be a different version than the ones the helper started with.
    Host, port and HTTPS are read from the database on every try: the update may have
    just migrated or restored it.
    p610: once `hurry` says a SIGTERM came (also in the middle of the wait), the wait ends `hurry_s` after that
    at the latest; SIGKILL follows 90 s after the signal, and the final state must be written before.
    """
    # Our own service on this machine, usually with a self-made certificate: not verified.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    # No proxy for a request to ourselves, whatever the environment says.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )

    def healthy_once() -> bool:
        try:
            url = _read_address(db_path) + "/healthz"
            with opener.open(url, timeout=5.0) as response:
                return response.status == 200 and json.loads(response.read()) == {"status": "ok"}
        except Exception:  # noqa: BLE001 - not reachable yet is the normal case here
            return False

    def wait() -> bool:
        deadline = time.monotonic() + timeout_s
        hurried = False
        while True:
            if not hurried and hurry is not None and hurry():
                hurried = True
                deadline = min(deadline, time.monotonic() + hurry_s)
            if healthy_once():
                return True
            if time.monotonic() + interval_s > deadline:
                return False
            time.sleep(interval_s)

    return wait
