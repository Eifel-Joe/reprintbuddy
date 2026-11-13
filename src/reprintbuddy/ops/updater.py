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
    prune_backups,
    restore_database,
)
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
)
from reprintbuddy.ops.runner import CommandResult, CommandRunner
from reprintbuddy.ops.service import RESTART_WAIT_CMD, START_CMD, STOP_CMD
from reprintbuddy.ops.status import UpdateStatus, is_busy, mark_interrupted, read_status, write_status
from reprintbuddy.ops.versions import is_newer

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
) -> UpdateStatus:
    data_dir = db_path.parent
    started = now()

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
        return _run_update(
            db_path, install_dir, runner, wait_healthy, now, http_get, download, target, keep_backups, previous, started
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
        )
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
        if info["backup"]:
            # After a failed rollback the message names this backup as the one to restore.
            protect = [Path(info["backup"])] if state == "rollback_failed" else []
            prune_backups(backup_dir, keep_backups, protect=protect)
        return put(state, step, message, finished=True, reason=reason, params=params)

    # --- before anything is changed: a failure leaves everything as it was -------------
    # v0.4.9 p500: what is installed comes from `release.json` (a package installation) or, for an
    # installation made with git, from git; where the new version comes from is the release list.
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

    def discard_download() -> None:
        shutil.rmtree(work_dir, ignore_errors=True)

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

    # --- from pip on, any failure is rolled back ---------------------------------------
    current = "install"

    def forward() -> tuple[str, str, str, dict[str, str]] | None:
        """None when everything went through, else (step, English line, reason code, parameters)."""
        nonlocal current
        current = "install"
        put("running", "install")
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
        done = runner((py, "-m", "pip", "install", "--no-cache-dir", "--upgrade", str(new_source)), timeout=900.0)
        if not done.ok:
            too_old = _too_old_python(done)
            if too_old is not None:
                return "install", too_old[0], "python_too_old", too_old[1]
            return "install", _reason(done, "pip install failed"), "install_failed", {"detail": _reason(done, "")}

        current = "verify"
        put("running", "verify")
        done = runner((py, "-m", "reprintbuddy", "--version"))
        expected = "reprintbuddy " + goal[1:]
        if not done.ok or done.stdout.strip() != expected:
            seen = done.stdout.strip()[:80] or _reason(done, "no output")
            return "verify", f"expected '{expected}', got '{seen}'", "verify_failed", {"expected": expected, "seen": seen}

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

    def backward() -> bool:
        """Previous environment, previous database, restart. True only if the old version answers."""
        ok = True
        if snapshot_taken and snapshot_dir.is_dir():
            # The environment as it was before pip: no network, no reinstall needed.
            try:
                shutil.rmtree(venv_dir, ignore_errors=True)
                snapshot_dir.rename(venv_dir)
            except OSError:
                ok = False
            done = CommandResult(0)
        else:
            shutil.rmtree(installed.source_dir / "build", ignore_errors=True)
            done = runner(
                (py, "-m", "pip", "install", "--no-cache-dir", "--upgrade", str(installed.source_dir)), timeout=900.0
            )
        if not done.ok:
            # No network for pip is the usual reason the update failed, so the reinstall of
            # the old version fails too. What matters is whether the old version is the one
            # installed and answering, not pip's exit code.
            old = info["from_tag"]
            shown = runner((py, "-m", "reprintbuddy", "--version"))
            ok &= bool(old) and shown.ok and shown.stdout.strip() == "reprintbuddy " + str(old)[1:]
        # The new version's folder goes; `release.json` was never touched, so it still names the old one.
        shutil.rmtree(new_source, ignore_errors=True)
        # Restore the database while nothing has it open. If the service cannot be stopped it
        # may still run the new code: then a restart (below) is the way back.
        stopped = runner(STOP_CMD, timeout=120.0)
        try:
            restore_database(backup, db_path)
        except (OSError, sqlite3.Error):
            ok = False
        done = runner(START_CMD if stopped.ok else RESTART_WAIT_CMD, timeout=120.0)
        ok &= done.ok
        return bool(wait_healthy()) and ok

    try:
        problem = forward()
    except Exception as error:  # noqa: BLE001 - whatever happened, the rollback must still run
        problem = (current, f"unexpected error: {type(error).__name__}", "unexpected", {"detail": type(error).__name__})
    if problem is None:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        _settle(install_dir, installed, goal, sha256)
        return finish("succeeded", "done", "")

    cause = f"{problem[0]}: {problem[1]}"
    put("running", "rollback", cause, reason=problem[2], params=problem[3])
    try:
        restored = backward()
    except Exception:  # noqa: BLE001
        restored = False
    if restored:
        return finish("rolled_back", "rollback", cause, problem[2], problem[3])
    return finish("rollback_failed", "rollback", f"{cause} – backup: {backup}", problem[2], problem[3])


def _settle(install_dir: Path, installed: Installation, goal: str, sha256: str) -> None:
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
        removed, failed = remove_git_copy(install_dir)
        for name in removed:
            print(f"update: removed the old git copy: {install_dir / name}", flush=True)
        for name in failed:
            print(f"update: could not remove the old git copy: {install_dir / name}", flush=True)
    keep = {goal} | ({installed.tag} if installed.tag else set())
    for name in prune_releases(install_dir, keep=keep):
        print(f"update: removed the unpacked version {name}", flush=True)


def interrupt_handler(db_path: Path, *, now: Callable[[], datetime]) -> Callable[[int, object], None]:
    """A SIGTERM handler for the helper: systemd stops a unit that runs past its timeout.

    Without it the state file would stay at "running" and the page would never say what
    happened. The handler records the final state and exits.
    """

    def handle(signum: int, frame: object) -> None:
        try:
            mark_interrupted(db_path.parent, now())
        except OSError:
            pass  # nothing more can be done about a status file that cannot be written
        raise SystemExit(128 + signum)

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
    db_path: Path, *, timeout_s: float = 90.0, interval_s: float = 2.0
) -> Callable[[], bool]:
    """A function that waits until the service answers `/healthz`, or gives up.

    Only the standard library is used (no SQLAlchemy, no httpx): pip replaces the
    installed packages while the helper runs, and a module imported lazily afterwards
    could be a different version than the ones the helper started with.
    Host, port and HTTPS are read from the database on every try: the update may have
    just migrated or restored it.
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
        while True:
            if healthy_once():
                return True
            if time.monotonic() + interval_s > deadline:
                return False
            time.sleep(interval_s)

    return wait
