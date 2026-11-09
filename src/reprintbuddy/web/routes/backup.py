"""Full backup and restore on the settings page (Spec 11.6).

Downloading is a POST: the file holds every secret, so it is made only on a button press
with a CSRF token, never by a link a browser could prefetch.

Restoring takes two steps. The upload is checked at once (shape, manifest, version) and
waits in the work folder; the page then says what the file is and asks for the password
if it is encrypted. Only the confirmation decrypts, checks the database, backs up the
running one and replaces it. The upload waits for one hour at most, one per browser; the
page carries its token, so a second tab with another upload cannot swap the file under it.

While the database is replaced and until the restart, `app.state.restore` pauses every
write (see `ops.update_guard`) and the engine (its `update_running` is true). An update
cannot start meanwhile, and a restore cannot start while an update runs: both decisions
are taken without an `await` between the check and setting the state.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from reprintbuddy.activity.writer import current_marks, write_restored_marker
from reprintbuddy.auth import MIN_PASSWORD_LENGTH
from reprintbuddy.db import known_revisions
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.ops import running
from reprintbuddy.ops.server import address_changed, base_url, effective_from_settings
from reprintbuddy.ops.runner import in_thread
from reprintbuddy.ops.service import request_restart
from reprintbuddy.ops.status import read_status
from reprintbuddy.web.bodylimit import MULTIPART_OVERHEAD, upload_length_ok
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_db, render
from reprintbuddy.web.routes.auth import password_confirmed
from reprintbuddy.web.routes.ops import _display_host

log = logging.getLogger("reprintbuddy.ops")
router = APIRouter()

MAX_PASSWORD_LENGTH = 1024
SESSION_KEY = "restore_upload"
UPLOAD_MAX_AGE = fb.STALE_AFTER
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


@dataclass
class RestoreState:
    """`running` from the decision to replace until the database is replaced; then
    `restart_pending` until the process ends (signing in and the restart button work again,
    in case the restart does not happen)."""

    phase: Literal["idle", "running", "restart_pending"] = "idle"

    @property
    def active(self) -> bool:
        return self.phase != "idle"


class _ReplaceGate:
    """Settles, between the event loop and the replace worker, who came first - one of them, never both.

    A cancelled request cannot stop a worker that was handed to a thread, and a thread may start just
    as the cancellation arrives. Either the worker `begin`s (then the database may be replaced and the
    route must not call the restore undone) or the route `abort`s first (then the worker replaces
    nothing and the route may remove the candidate). Under the lock exactly one of the two succeeds.

    p164 (v0.3.14): the same lock settles who asks for the restart once the database is replaced. A
    cancellation that arrives while the worker replaces leaves the route nothing to wait for; the worker
    `finish`es and learns that the request is gone, so it asks for the restart itself. If the worker
    finished first, `abort` says so ("finished") and the route does it, also when it is cancelled just then.
    Never both, never neither: the restart must happen once (Spec 11.6).

    Review of v0.3.14 D, finding 3: a worker that fails after the route gave up has replaced nothing; the
    restore state goes back to "idle" (writes go on), whichever of the two comes first. The gate sets the
    phase of a given-up restore itself, under its lock, so the route and the worker cannot overwrite each
    other's verdict.
    """

    def __init__(self, state: RestoreState) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._begun = False
        self._aborted = False
        self._finished = False
        self._failed = False

    def begin(self) -> bool:
        with self._lock:
            if self._aborted:
                return False
            self._begun = True
            return True

    def abort(self) -> Literal["not_begun", "running", "finished", "failed"]:
        """The route gives up. "not_begun": the worker never will (the route removes the candidate);
        "running": the worker replaces and asks for the restart itself when done; "finished": the route asks
        for it; "failed": nothing was replaced. The restore state is set to fit."""
        with self._lock:
            self._aborted = True
            if not self._begun or self._failed:
                self._state.phase = "idle"
                return "failed" if self._failed else "not_begun"
            # The worker may have replaced the database, or still does: the old process holds state of the
            # old one. Writes stay paused and sign-in and the restart button work (as after a failed restart).
            self._state.phase = "restart_pending"
            return "finished" if self._finished else "running"

    def finish(self) -> bool:
        """The worker has replaced the database; True when the route gave up before (the worker restarts)."""
        with self._lock:
            self._finished = True
            return self._aborted

    def fail(self) -> None:
        """The worker failed before the database was replaced."""
        with self._lock:
            self._failed = True
            if self._aborted:
                self._state.phase = "idle"  # nobody waits for the error: nothing was replaced


_restarts: set[asyncio.Task] = set()  # strong references: a task nobody awaits any more is not collected


def _restart_finished(task: asyncio.Task) -> None:
    _restarts.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.warning("restart after restore failed: %s", type(error).__name__)
    elif not task.result().ok:
        log.warning("restart after restore failed: %s", (task.result().stderr or "").strip()[:300])


async def _restart_after_restore(runner):
    """Ask for the restart once the database is replaced, whether or not the request is still awaited.

    p157 (v0.3.12). Root: through the app's middleware a cancelled request (a closed browser) reaches the
    route only after the replace worker has ended, at the next wait - the one for this restart, already
    handed to a thread. Cancelling that wait dropped the restart if no thread had picked it up yet, and let
    it run if one had: the same closed browser restarted the service or left it running on the old state,
    by chance (CI, under load). Spec 11.6: the restore replaces the database and restarts; from here on the
    restart belongs to the replace, not to the browser. So it runs as a task of its own that the
    cancellation of the request does not reach (`shield`); its failure is logged by the task itself, as
    nobody may be waiting for it.
    Dropped: checking for the cancellation before handing the restart over - it arrives only at the wait,
    so there is nothing to check before it; and "never restart once cancelled" - the old process would keep
    running on the state of the old database until someone presses the restart button.
    Test: tests/web/test_backup_abandon.py::test_cancelling_once_the_replace_worker_has_begun_leaves_the_restore_pending
    """
    return await asyncio.shield(_start_restart(runner))


def _start_restart(runner) -> asyncio.Task:
    """The restart as a task of its own, kept alive and logged by itself (nobody may wait for it)."""
    task = asyncio.ensure_future(in_thread(request_restart, runner))  # sudo may take seconds
    _restarts.add(task)
    task.add_done_callback(_restart_finished)
    return task


def _restart_from_worker(runner) -> None:
    """The restart, asked for by the replace worker when nobody waits for the request any more (p164).

    Called in the worker's thread: it blocks for the sudo call (up to 15 s) there, not in the event loop.
    """
    try:
        result = request_restart(runner)
    except Exception as error:  # noqa: BLE001 - nobody waits: the log is the only place left
        log.warning("restart after restore failed: %s", type(error).__name__)
        return
    if not result.ok:
        log.warning("restart after restore failed: %s", (result.stderr or "").strip()[:300])


def _now() -> datetime:
    return datetime.now(UTC)


# The fixed reasons a refused restore is logged with (Spec 10.7, group 4); anything else is "failed".
RESTORE_REASONS = frozenset({"foreign", "too_new", "too_large", "damaged", "incompatible", "failed", "busy"})


def _refused(request: Request, code: str) -> RedirectResponse:
    """Back to the card with the message of `code`, and the refusal in the activity log."""
    reason = code if code in RESTORE_REASONS else "failed"
    request.app.state.activity.record("restore_rejected", "user", {"reason": reason})
    return _back(f"restore_{code}", "ops-restore")


def _back(code: str | None, anchor: str) -> RedirectResponse:
    query = f"&ops={code}" if code else ""
    return RedirectResponse(f"/settings?tab=maintenance{query}#{anchor}", status_code=303)


def _work_dir(request: Request) -> Path:
    return fb.work_dir_for(request.app.state.db_path)


def _remove(*paths: Path) -> None:
    for path in paths:
        fb.remove_database_files(path)


# --- downloading -----------------------------------------------------------------------------


class RemovedAfterSending(FileResponse):
    """A file response that deletes its file when the answer is over, however it ends (p103).

    The file of a full backup holds every secret. Root: it used to be deleted by the response's
    background task (`BackgroundTask`), and Starlette runs that only after the file was sent to the
    end: a send that fails (a connection that errors out), a cancelled request (shutdown) or an
    exception in between skipped it, and the file lay in the work folder until the next cleanup
    (a page after an hour, or the next start). `finally` does not depend on how the answer ended.
    Not reproduced: a browser that merely cancels the download of a real Uvicorn server; Uvicorn
    swallows the lost connection and the answer ends normally. Rejected: deleting the file before
    sending, with the file kept open (works on Linux only, and the size would be unknown to
    `FileResponse`). Tests: test_backup_download_cleanup.py.
    """

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            _remove(Path(self.path))


@router.post("/settings/backup", dependencies=[Depends(verify_csrf)])
async def download_backup(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    password = str(form.get("backup_password") or "")
    if password != str(form.get("backup_password_repeat") or ""):
        return _back("backup_mismatch", "ops-backup")
    if password and not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        return _back("backup_length", "ops-backup")
    login = str(form.get("login_password") or "")[: 4 * MAX_PASSWORD_LENGTH]
    if not await password_confirmed(request, db, login):
        return _back("backup_login", "ops-backup")
    work = fb.ensure_work_dir(_work_dir(request))
    fb.clear_work_dir(work, older_than=fb.STALE_AFTER)
    # v0.3.12 p149: the route names the file and holds a flag, as the restore does (p140). Root: a
    # cancellation that reaches `run_in_threadpool` at once (no middleware that waits for the thread, as the
    # app's does today) left the worker writing a file with every secret that nobody knew to delete until
    # the next cleanup; `RemovedAfterSending` is never made then. Tests: test_backup_abandon.py "p149".
    target = work / f"backup-{secrets.token_hex(8)}.zip"
    abandoned = threading.Event()
    try:
        # Argon2 needs 64 MiB and noticeable time: through the limiter, like a login.
        created = await request.app.state.hash_limiter.run(
            fb.create_backup,
            request.app.state.db_path,
            work_dir=work,
            password=password or None,
            now=_now(),
            abandoned=abandoned,
            target=target,
        )
    except (OSError, sqlite3.Error, fb.BackupError) as error:
        log.warning("backup failed: %s", type(error).__name__)  # never the message: no paths, no values
        return _back("backup_failed", "ops-backup")
    except BaseException:  # cancelled (a closed browser, a shutdown): the flag first, then the file
        abandoned.set()
        _remove(target)
        raise
    request.app.state.activity.record("backup_downloaded", "user", {"encrypted": bool(password)})
    return RemovedAfterSending(
        created.path,
        media_type="application/zip",
        filename=created.filename,
        headers={"Cache-Control": "no-store"},
    )


# --- restoring -------------------------------------------------------------------------------


def _staged_token(request: Request) -> str | None:
    info = request.session.get(SESSION_KEY)
    token = info.get("token") if isinstance(info, dict) else None
    return token if isinstance(token, str) and _TOKEN.fullmatch(token) else None


def _staged(request: Request) -> Path | None:
    """The upload of this browser that waits for confirmation, if it is still valid."""
    info = request.session.get(SESSION_KEY)
    token = _staged_token(request)
    at = info.get("at") if isinstance(info, dict) else None
    if token is None or not isinstance(at, str):
        return None
    path = _work_dir(request) / f"upload-{token}.zip"
    try:
        expired = _now() - datetime.fromisoformat(at) > UPLOAD_MAX_AGE
    except (TypeError, ValueError):
        expired = True
    if expired:
        _remove(path)
        return None
    return path if path.is_file() else None


def _discard(request: Request) -> None:
    path = _staged(request)
    request.session.pop(SESSION_KEY, None)
    if path is not None:
        _remove(path)


def _protected(request: Request) -> list[Path]:
    """A backup named by a failed rollback must survive the pruning (as in the update)."""
    status = read_status(request.app.state.db_path.parent)
    return [Path(status.backup)] if status.state == "rollback_failed" and status.backup else []


@router.post("/settings/restore/upload")
async def restore_upload(request: Request) -> Response:
    # The size is checked before anything reads the body: the CSRF check would parse the
    # whole form, and Starlette keeps file parts of any size in a temporary file.
    # `upload_length_ok`: no `Transfer-Encoding` (h11 would frame by it, not by the length) and a
    # plain length within the limit; the middleware counts what is read (v0.3.9 T0).
    if not upload_length_ok(request, fb.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD):
        refused = _back("restore_too_large", "ops-restore")
        # Nothing reads the body: close the connection, or the server reads and drops all of it
        # before the browser follows the redirect (v0.3.9 review, sister of the print-file upload).
        refused.headers["Connection"] = "close"
        return refused
    # Parsed once, with limits; `verify_csrf` reads the same form (Starlette keeps it).
    form = await request.form(max_files=1, max_fields=4)
    upload = form.get("backup_file")
    try:
        await verify_csrf(request)
    except HTTPException:
        if isinstance(upload, UploadFile):
            await upload.close()  # a refused token leaves no temporary file behind
        raise
    if not isinstance(upload, UploadFile) or not upload.filename:
        return _back("restore_nofile", "ops-restore")
    # The guard let this request in before the body was read; a restore may have begun since.
    # No `await` from here to the cleanup: its candidate is never deleted under it.
    if request.app.state.restore.active:
        await upload.close()
        return _refused(request, "busy")

    _discard(request)  # the earlier upload of this browser; other browsers keep theirs
    work = fb.ensure_work_dir(_work_dir(request))
    fb.clear_work_dir(work, older_than=fb.STALE_AFTER)
    token = secrets.token_urlsafe(24)
    target = work / f"upload-{token}.zip"
    # v0.3.12 p149, sister of the download: an unencrypted backup holds every secret, and a cancellation
    # that reaches `run_in_threadpool` at once left the saved file behind (above all while the manifest was
    # read: the file was complete). The flag stops the saving thread; the line below removes the file.
    abandoned = threading.Event()
    try:
        await run_in_threadpool(
            fb.save_upload, upload.file, target, limit=fb.MAX_UPLOAD_BYTES, abandoned=abandoned
        )
        await run_in_threadpool(fb.read_manifest, target)
    except fb.BackupError as error:
        _remove(target)
        return _refused(request, error.code)
    except OSError as error:
        _remove(target)
        log.warning("restore upload failed: %s", type(error).__name__)
        return _refused(request, "failed")
    except BaseException:  # cancelled (a closed browser, a shutdown): the flag first, then the file
        abandoned.set()
        _remove(target)
        raise
    finally:
        try:
            await upload.close()  # a spooled upload is closed in a worker thread: one more await
        except BaseException:  # cancelled while closing: the saved file has no session entry, nobody keeps it
            _remove(target)
            raise
    # Not covered (Windows only, not a supported host): a cancellation while `read_manifest` still has the
    # file open; the removal above fails there and the file stays until the stale cleanup (an hour).
    request.session[SESSION_KEY] = {"token": token, "at": _now().isoformat()}
    return RedirectResponse("/settings/restore", status_code=303)


def _confirm_page(
    request: Request, manifest: fb.Manifest, error: str | None = None, field: str | None = None
) -> Response:
    return render(
        request,
        "restore_confirm.html",
        manifest=manifest,
        upload_token=_staged_token(request),
        older=manifest.schema_revision != known_revisions()[-1],
        error=error,
        error_field=field,
        active_nav="settings",
    )


@router.get("/settings/restore")
async def restore_page(request: Request) -> Response:
    path = _staged(request)
    if path is None:
        return _back(None, "ops-restore")
    try:
        manifest = await run_in_threadpool(fb.read_manifest, path)
    except (fb.BackupError, OSError):
        _discard(request)
        return _back("restore_expired", "ops-restore")
    return _confirm_page(request, manifest)


@router.post("/settings/restore/cancel", dependencies=[Depends(verify_csrf)])
async def restore_cancel(request: Request) -> Response:
    _discard(request)
    request.app.state.activity.record("restore_cancelled", "user")
    return _back(None, "ops-restore")


@router.post("/settings/restore", dependencies=[Depends(verify_csrf)])
async def restore_confirm(request: Request, db: Session = Depends(get_db)) -> Response:
    app = request.app
    form = await request.form()
    path = _staged(request)
    expected = _staged_token(request)
    shown = str(form.get("upload_token") or "")
    # The file this page showed, not one another tab uploaded in the meantime.
    if path is None or expected is None or not secrets.compare_digest(shown.encode(), expected.encode()):
        return _back("restore_expired", "ops-restore")
    password = str(form.get("backup_password") or "")[: 4 * MAX_PASSWORD_LENGTH]
    login = str(form.get("login_password") or "")[: 4 * MAX_PASSWORD_LENGTH]
    if not await password_confirmed(request, db, login):
        try:
            manifest_shown = await run_in_threadpool(fb.read_manifest, path)
        except (fb.BackupError, OSError):
            _discard(request)
            return _back("restore_expired", "ops-restore")
        return _confirm_page(request, manifest_shown, "ops.restore.login_wrong", "login")  # the upload stays
    work = fb.ensure_work_dir(_work_dir(request))
    candidate = work / f"candidate-{secrets.token_hex(8)}.db"

    manifest: fb.Manifest | None = None
    # Set when the request is cancelled: a cancellation does not stop the worker threads, they would
    # finish the decrypted copy after the line below removed it (v0.3.11 p140). They watch this flag.
    abandoned = threading.Event()
    try:
        manifest = await run_in_threadpool(fb.read_manifest, path)
        await app.state.hash_limiter.run(
            fb.extract_database, path, manifest, password, candidate, abandoned=abandoned
        )
        await run_in_threadpool(fb.verify_database, candidate, manifest, work_dir=work, abandoned=abandoned)
    except (fb.PasswordNeeded, fb.WrongPassword) as error:
        _remove(candidate)
        assert manifest is not None
        return _confirm_page(request, manifest, f"ops.restore.{error.code}", "backup")  # the upload stays
    except fb.BackupError as error:
        _remove(candidate)
        _discard(request)
        return _refused(request, error.code)
    except Exception as error:  # noqa: BLE001 - a full disk in a migration, say: nothing was changed
        _remove(candidate)
        log.warning("restore check failed: %s", type(error).__name__)
        if manifest is not None and fb.is_environmental(error):
            # The machine failed, not the file (p141): the upload stays, the page asks to try again.
            return _confirm_page(request, manifest, "ops.restore.retry", "retry")
        _discard(request)
        return _refused(request, "failed")
    except BaseException:  # cancelled (a closed browser, a shutdown): the flag first, then the file
        abandoned.set()
        _remove(candidate)
        raise

    # The state is set first and the update asked about afterwards (in a worker thread, v0.3.8 p96):
    # from the check of `active` to setting it there is no `await`, so a second restore or an update
    # starting meanwhile finds the restore running and is refused.
    state: RestoreState = app.state.restore
    data_dir = app.state.db_path.parent
    if state.active or app.state.update_starting:  # a click on "Update" whose sudo is still on its way
        _remove(candidate)
        return _refused(request, "busy")
    state.phase = "running"
    try:
        update_busy = await running.update_running_async(data_dir, app.state.ops.runner, _now())
    except BaseException:  # cancelled (shutdown) or failing: nothing is replaced
        state.phase = "idle"
        _remove(candidate)
        raise
    if update_busy:
        state.phase = "idle"
        _remove(candidate)
        return _refused(request, "busy")
    # A write that passed the guard just before this line may still be under way; it would
    # land in the restored database with what it read from the old one. This request is one.
    deadline = time.monotonic() + app.state.restore_write_wait
    try:
        while app.state.writes_in_flight > 1:
            if time.monotonic() > deadline:
                state.phase = "idle"
                _remove(candidate)
                return _refused(request, "busy")
            await asyncio.sleep(0.02)
    except BaseException:  # cancelled while waiting (shutdown, …): nothing is replaced yet
        state.phase = "idle"
        _remove(candidate)
        raise
    gate = _ReplaceGate(state)

    def replace() -> None:
        if not gate.begin():  # the request was cancelled before this thread got going: replace nothing
            return
        try:
            marks = current_marks(app.state.db_path)  # p186: the log's marks belong to what has happened
            fb.replace_database(candidate, app.state.db_path, now=_now(), protect=_protected(request))
        except BaseException:
            _remove(candidate)  # nothing was replaced: the decrypted copy does not wait for the next start
            gate.fail()
            raise
        try:
            # The restored database is migrated at the next start; that start logs the restore into it.
            write_restored_marker(
                data_dir, created_at=manifest.created_at, version=manifest.app_version, via=None, marks=marks
            )
        except Exception as error:  # noqa: BLE001 - p164: the restore has happened; only its log entry is lost
            log.warning("restore note for the activity log not written: %s", type(error).__name__)
        finally:
            # p164: the database is replaced. Was the request cancelled meanwhile, nobody else asks for the
            # restart (see `_ReplaceGate`); the old process must not go on with the state of the old database.
            # Review v0.3.14 D, finding 2: uvicorn cancels a running request only when it stops. A restart
            # asked for then would bring the service back against `systemctl stop`; the next start uses the
            # restored database and logs the restore anyway.
            if gate.finish() and not app.state.stopping:
                _restart_from_worker(app.state.ops.runner)

    try:
        async with app.state.ticker.hold():
            await run_in_threadpool(replace)
    except Exception as error:  # noqa: BLE001 - it fails before or in the one-step copy: nothing changed
        state.phase = "idle"
        _remove(candidate)
        log.warning("restore failed: %s", type(error).__name__)
        if fb.is_environmental(error):
            return _confirm_page(request, manifest, "ops.restore.retry", "retry")  # p141: the upload stays
        _discard(request)
        return _refused(request, "failed")
    except BaseException:  # cancelled (shutdown, a closed browser) while waiting for the tick or replacing
        outcome = gate.abort()  # sets the restore state to fit (see `_ReplaceGate`)
        if outcome == "not_begun":  # the worker has not begun and now never will
            _remove(candidate)
        elif outcome == "finished" and not app.state.stopping:
            # p164: the worker finished while the request was still there, so it left the restart to the
            # route; the cancellation must not drop it. A worker still replacing restarts by itself.
            _start_restart(app.state.ops.runner)
        raise
    # The old process holds state of the old database in memory (engine, rights): writes
    # and the engine stay paused until it is gone. Signing in and the restart button work,
    # in case the restart below fails or systemd takes it and nothing happens.
    state.phase = "restart_pending"
    _discard(request)

    # Address, port and HTTPS come with the backup: say where the service answers next.
    new_url = None
    running_server = app.state.ops.effective
    try:
        if running_server is not None:
            saved = effective_from_settings(db)
            if address_changed(saved, running_server):
                new_url = base_url(_display_host(request, saved), saved.port, saved.tls)
    except Exception as error:  # noqa: BLE001 - only a hint; the restart must be asked for anyway
        log.warning("address after restore unreadable: %s", type(error).__name__)
    result = await _restart_after_restore(app.state.ops.runner)
    return render(request, "restored.html", restarted=result.ok, new_url=new_url, active_nav="settings")
