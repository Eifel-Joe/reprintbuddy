"""The update helper: back up, switch to the newest release, roll back on any failure.

It runs as its own process (`reprintbuddy update`, started by the systemd unit
`reprintbuddy-update.service`), never inside the web service: the service is
restarted midway and would take a child process down with it.

All imports are at the top of this module on purpose. pip replaces the package files
while the helper runs, and the rollback must not load any new code.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker
from reprintbuddy.ops.backup import (
    KEEP_BACKUPS,
    backup_database,
    backup_dir_for,
    prune_backups,
    restore_database,
)
from reprintbuddy.ops.runner import CommandResult, CommandRunner
from reprintbuddy.ops.server import base_url, connect_host
from reprintbuddy.ops.service import RESTART_WAIT_CMD
from reprintbuddy.ops.status import UpdateStatus, is_busy, mark_interrupted, read_status, write_status
from reprintbuddy.ops.versions import is_newer, local_tags, newest_release, parse_release_tag

MAX_REASON = 300


def venv_python(install_dir: Path) -> Path:
    return install_dir / ".venv" / "bin" / "python"


def _reason(result: CommandResult, fallback: str) -> str:
    """The first line the command complained with, short enough for a page."""
    for text in (result.stderr, result.stdout):
        lines = (text or "").strip().splitlines()
        if lines:
            return lines[0][:MAX_REASON]
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
        write_status(data_dir, status)
        print(f"update: {step}: {state}" + (f" ({message})" if message else ""), flush=True)
        return status

    def finish(state: str, step: str, message: str) -> UpdateStatus:
        if info["backup"]:
            prune_backups(backup_dir, keep_backups)
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
    fetched = runner((*git, "fetch", "--tags", "origin"), timeout=300.0)
    if not fetched.ok:
        return finish("failed", "fetch", "fetch: " + _reason(fetched, "git fetch failed"))
    tags = local_tags(install_dir, runner)
    goal = target or newest_release(tags)
    if goal is None and not target:
        return finish("failed", "fetch", "fetch: no newer release")
    if goal is None or parse_release_tag(goal) is None or goal not in tags:
        return finish("failed", "fetch", f"fetch: unknown target {target}")
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
        done = runner((py, "-m", "pip", "install", "--no-cache-dir", "--upgrade", d), timeout=900.0)
        if not done.ok:
            # No network for pip is the usual reason the update failed, so the reinstall of
            # the old version fails too. What matters is whether the old version is the one
            # installed and answering, not pip's exit code.
            old = info["from_tag"]
            shown = runner((py, "-m", "reprintbuddy", "--version"))
            ok &= bool(old) and shown.ok and shown.stdout.strip() == "reprintbuddy " + str(old)[1:]
        try:
            restore_database(backup, db_path)
        except (OSError, sqlite3.Error):
            ok = False
        done = runner(RESTART_WAIT_CMD, timeout=120.0)
        ok &= done.ok
        return bool(wait_healthy()) and ok

    try:
        problem = forward()
    except Exception as error:  # noqa: BLE001 - whatever happened, the rollback must still run
        problem = (current, f"unexpected error: {type(error).__name__}")
    if problem is None:
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
        mark_interrupted(db_path.parent, now())
        raise SystemExit(128 + signum)

    return handle


def http_health_waiter(
    db_path: Path, *, timeout_s: float = 90.0, interval_s: float = 2.0
) -> Callable[[], bool]:
    """A function that waits until the service answers `/healthz`, or gives up.

    Host, port and HTTPS are read from the database on every try: the update may
    have just migrated or restored it.
    """

    def healthy_once() -> bool:
        try:
            engine = make_engine(db_path)
            try:
                with make_sessionmaker(engine)() as session:
                    host = str(st.get_setting(session, "host"))
                    port = int(st.get_setting(session, "port"))  # type: ignore[arg-type]
                    tls = st.tls_enabled(session)
            finally:
                engine.dispose()
            url = base_url(connect_host(host), port, tls) + "/healthz"
            response = httpx.get(url, verify=False, timeout=5.0)  # noqa: S501 - our own service, on this machine
            return response.status_code == 200 and response.json() == {"status": "ok"}
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
