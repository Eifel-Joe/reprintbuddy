"""The update helper: back up, switch to the newest release, roll back on any failure.

It runs as its own process (`reprintbuddy update`, started by the systemd unit
`reprintbuddy-update.service`), never inside the web service: the service is
restarted midway and would take a child process down with it.

All imports are at the top of this module on purpose. pip replaces the package files
while the helper runs, and the rollback must not load any new code.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import ssl
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from reprintbuddy.ops.backup import (
    KEEP_BACKUPS,
    backup_database,
    backup_dir_for,
    prune_backups,
    restore_database,
)
from reprintbuddy.ops.runner import CommandResult, CommandRunner
from reprintbuddy.ops.service import RESTART_WAIT_CMD, START_CMD, STOP_CMD
from reprintbuddy.ops.status import UpdateStatus, is_busy, mark_interrupted, read_status, write_status
from reprintbuddy.ops.versions import is_newer, local_tags, newest_release, parse_release_tag

MAX_REASON = 300


def venv_python(install_dir: Path) -> Path:
    return install_dir / ".venv" / "bin" / "python"


_NOISE_LINES = ("[SQL:", "[parameters:", "(Background on this error")
_HEADLINE = re.compile(r"^(fatal|error)\b", re.IGNORECASE)


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


def run_update(
    db_path: Path,
    *,
    install_dir: Path,
    runner: CommandRunner,
    wait_healthy: Callable[[], bool],
    now: Callable[[], datetime],
    target: str | None = None,
    keep_backups: int = KEEP_BACKUPS,
) -> UpdateStatus:
    data_dir = db_path.parent
    backup_dir = backup_dir_for(db_path)
    started = now()

    previous = read_status(data_dir)
    if previous.state == "running" and is_busy(previous, started):
        print("update: another update is running, nothing done", flush=True)
        return previous

    d = str(install_dir)
    py = str(venv_python(install_dir))
    git = ("git", "-C", d)
    venv_dir = install_dir / ".venv"
    snapshot_dir = install_dir / ".venv.rollback"
    snapshot_taken: list[bool] = []  # only a snapshot of THIS run may be restored, never a leftover
    info: dict[str, str | None] = {
        "from_tag": None,
        "to_tag": target or (previous.to_tag if previous.state == "queued" else None),
        "backup": None,
    }

    def put(state: str, step: str, message: str = "", *, finished: bool = False) -> UpdateStatus:
        status = UpdateStatus(
            state=state,  # type: ignore[arg-type]
            step=step,
            from_tag=info["from_tag"],
            to_tag=info["to_tag"],
            started_at=started,
            finished_at=now() if finished else None,
            message=message,
            backup=info["backup"],
        )
        try:
            write_status(data_dir, status)
        except OSError as error:
            # A full disk or a read-only file must not stop an update half way, least of all
            # the rollback: the page just keeps the last state it could get.
            print(f"update: cannot write the status file ({error})", flush=True)
        print(f"update: {step}: {state}" + (f" ({message})" if message else ""), flush=True)
        return status

    def finish(state: str, step: str, message: str) -> UpdateStatus:
        if info["backup"]:
            # After a failed rollback the message names this backup as the one to restore.
            protect = [Path(info["backup"])] if state == "rollback_failed" else []
            prune_backups(backup_dir, keep_backups, protect=protect)
        return put(state, step, message, finished=True)

    # --- before anything is changed: a failure leaves everything as it was -------------
    put("running", "backup")
    described = runner((*git, "describe", "--tags", "--exact-match", "HEAD"))
    info["from_tag"] = described.stdout.strip() or None if described.ok else None
    head = runner((*git, "rev-parse", "HEAD"))
    from_ref = head.stdout.strip() if head.ok else ""
    if not from_ref:
        return finish("failed", "backup", "backup: " + _reason(head, "cannot read the installed commit"))
    try:
        backup = backup_database(
            db_path, backup_dir, label=info["from_tag"] or from_ref[:12], now=now()
        )
    except (OSError, sqlite3.Error) as error:
        return finish("failed", "backup", f"backup: {str(error)[:MAX_REASON]}")
    info["backup"] = str(backup)

    put("running", "fetch")
    # --force: a tag moved on GitHub ("would clobber existing tag") must not stop every update;
    # --prune-tags: a tag deleted there must not stay offered here.
    fetched = runner(
        (*git, "fetch", "--prune", "--prune-tags", "--force", "--tags", "origin"), timeout=300.0
    )
    if not fetched.ok:
        return finish("failed", "fetch", "fetch: " + _reason(fetched, "git fetch failed"))
    tags = local_tags(install_dir, runner)
    # The version the page offered (or the one asked for) is installed, not whatever is newest
    # by now; if it is gone, the update stops instead of installing something else.
    wanted = target or info["to_tag"]
    goal = wanted or newest_release(tags)
    if goal is None:
        return finish("failed", "fetch", "fetch: no newer release")
    if parse_release_tag(goal) is None or goal not in tags:
        return finish("failed", "fetch", f"fetch: unknown target {goal}")
    info["to_tag"] = goal
    if not is_newer(goal, info["from_tag"]):
        return finish("failed", "fetch", "fetch: no newer release")

    # --- from the checkout on, any failure is rolled back ------------------------------
    current = "checkout"

    def forward() -> tuple[str, str] | None:
        nonlocal current
        current = "checkout"
        put("running", "checkout")
        done = runner((*git, "checkout", "--quiet", "--detach", goal))
        if not done.ok:
            return "checkout", _reason(done, "git checkout failed")

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
                return "install", f"cannot back up the Python environment: {str(error)[:200]}"
        shutil.rmtree(install_dir / "build", ignore_errors=True)  # old build leftovers would bring back deleted modules
        done = runner((py, "-m", "pip", "install", "--no-cache-dir", "--upgrade", d), timeout=900.0)
        if not done.ok:
            return "install", _reason(done, "pip install failed")

        current = "verify"
        put("running", "verify")
        done = runner((py, "-m", "reprintbuddy", "--version"))
        expected = "reprintbuddy " + goal[1:]
        if not done.ok or done.stdout.strip() != expected:
            seen = done.stdout.strip()[:80] or _reason(done, "no output")
            return "verify", f"expected '{expected}', got '{seen}'"

        current = "migrate"
        put("running", "migrate")
        done = runner((py, "-m", "reprintbuddy", "migrate", "--db", str(db_path)), timeout=300.0)
        if not done.ok:
            return "migrate", _reason(done, "the database migration failed")

        current = "restart"
        put("running", "restart")
        done = runner(RESTART_WAIT_CMD, timeout=120.0)
        if not done.ok:
            return "restart", _reason(done, "the restart failed")

        current = "health"
        put("running", "health")
        if not wait_healthy():
            return "health", "the new version did not answer after the restart"
        return None

    def backward() -> bool:
        """Previous code, previous database, restart. True only if the old version answers."""
        ok = True
        done = runner((*git, "checkout", "--quiet", "--detach", from_ref))
        ok &= done.ok
        shutil.rmtree(install_dir / "build", ignore_errors=True)
        if snapshot_taken and snapshot_dir.is_dir():
            # The environment as it was before pip: no network, no reinstall needed.
            try:
                shutil.rmtree(venv_dir, ignore_errors=True)
                snapshot_dir.rename(venv_dir)
            except OSError:
                ok = False
            done = CommandResult(0)
        else:
            done = runner((py, "-m", "pip", "install", "--no-cache-dir", "--upgrade", d), timeout=900.0)
        if not done.ok:
            # No network for pip is the usual reason the update failed, so the reinstall of
            # the old version fails too. What matters is whether the old version is the one
            # installed and answering, not pip's exit code.
            old = info["from_tag"]
            shown = runner((py, "-m", "reprintbuddy", "--version"))
            ok &= bool(old) and shown.ok and shown.stdout.strip() == "reprintbuddy " + str(old)[1:]
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
        problem = (current, f"unexpected error: {type(error).__name__}")
    if problem is None:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        return finish("succeeded", "done", "")

    cause = f"{problem[0]}: {problem[1]}"
    put("running", "rollback", cause)
    try:
        restored = backward()
    except Exception:  # noqa: BLE001
        restored = False
    if restored:
        return finish("rolled_back", "rollback", cause)
    return finish("rollback_failed", "rollback", f"{cause} – backup: {backup}")


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
