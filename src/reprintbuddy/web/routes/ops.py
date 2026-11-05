"""Restart, update and export from the settings page.

Commands run only on a POST with a CSRF token. The one command a GET route may run
is the local `git describe` (once per process); GitHub is asked only on a button press.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from reprintbuddy import __version__
from reprintbuddy.ops.export import export_bytes
from reprintbuddy.ops.server import (
    EffectiveServer,
    address_changed,
    base_url,
    connect_host,
    effective_from_settings,
)
from reprintbuddy.ops.running import update_running
from reprintbuddy.ops.service import request_restart, start_update_unit
from reprintbuddy.ops.status import (
    BUSY_STATES,
    UPDATE_STEPS,
    UpdateStatus,
    is_busy,
    read_status,
    write_status,
)
from reprintbuddy.ops.versions import (
    UpdateCheckError,
    VersionInfo,
    check_versions,
    installed_tag,
    is_git_install,
)
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_db, render

log = logging.getLogger("reprintbuddy.ops")
router = APIRouter()

OPS_MESSAGES = frozenset(
    {"restart_failed", "check_failed", "busy", "no_update", "start_failed", "not_git"}
)

# POST routes that stay usable while an update runs: asking GitHub, the update button
# itself (it answers "busy" on its own), signing out, and the two buttons of the notice
# about a missing right (they ask Bambuddy and set a flag in the cookie, nothing in the
# database). Everything else is paused: the rollback restores the backup made before the
# update, so a booking made meanwhile would be lost.
UPDATE_EXEMPT_PATHS = frozenset(
    {"/settings/update/check", "/settings/update", "/logout", "/bambuddy/rights/later", "/bambuddy/rights/recheck"}
)
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass
class OpsView:
    running_url: str | None
    restart_pending: bool
    message: str | None  # translation key of a message, or None
    git_install: bool = False
    installed: str | None = None
    dev_version: str = __version__
    version_info: VersionInfo | None = None
    update: UpdateStatus = UpdateStatus()
    busy: bool = False
    interrupted: bool = False  # the state says "running" but the helper is gone
    state_key: str | None = None  # translation key for the update state
    step_key: str | None = None  # translation key for the running step


def _display_host(request: Request, effective: EffectiveServer) -> str:
    """The wildcard address is no address to open: use the one the browser used."""
    if effective.host in ("0.0.0.0", "::", ""):
        return request.url.hostname or connect_host(effective.host)
    return effective.host


def _data_dir(request: Request):
    return request.app.state.db_path.parent


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _installed(request: Request) -> str | None:
    """The tag this process runs: asked once, because every update restarts the process."""
    app = request.app
    ops = app.state.ops
    if not hasattr(app.state, "installed_tag"):
        app.state.installed_tag = installed_tag(ops.install_dir, ops.runner)
    return app.state.installed_tag


def ops_view(request: Request, session: Session) -> OpsView:
    app = request.app
    ops = app.state.ops
    effective = ops.effective
    running_url = None
    pending = False
    if effective is not None:
        running_url = base_url(_display_host(request, effective), effective.port, effective.tls)
        pending = effective_from_settings(session) != effective
    code = request.query_params.get("ops")
    message = f"ops.msg.{code}" if code in OPS_MESSAGES else None

    view = OpsView(running_url=running_url, restart_pending=pending, message=message)
    view.dev_version = __version__  # read now, so it is the version of the running code
    view.git_install = is_git_install(ops.install_dir)
    if view.git_install:
        view.installed = _installed(request)
        view.version_info = getattr(app.state, "version_info", None)
    view.update = read_status(_data_dir(request))
    view.busy = update_running(_data_dir(request), request.app.state.ops.runner, _utc_now())
    view.interrupted = view.update.state in BUSY_STATES and not view.busy
    if view.interrupted:
        view.state_key = "ops.update.state.interrupted"
    elif view.update.state != "idle":
        view.state_key = f"ops.update.state.{view.update.state}"
    if (view.busy or view.interrupted) and view.update.step in UPDATE_STEPS:
        view.step_key = f"ops.update.step.{view.update.step}"
    return view


async def update_guard(request: Request, call_next) -> Response:
    """Pause every write while an update runs (see `UPDATE_EXEMPT_PATHS`)."""
    if request.method in WRITE_METHODS and request.url.path not in UPDATE_EXEMPT_PATHS:
        if update_running(_data_dir(request), request.app.state.ops.runner, _utc_now()):
            return render(
                request,
                "error.html",
                status_code=503,
                layout="base.html",
                title_key="error.generic.title",
                message_key="ops.update.in_progress",
            )
    return await call_next(request)


@router.get("/settings/export")
def export(db: Session = Depends(get_db)):
    # A plain `def`: reading every table is blocking work and runs in the thread pool.
    now = _utc_now()
    body = export_bytes(db.connection(), now=now)
    name = f"reprintbuddy-export-{now:%Y%m%d-%H%M%S}.json"
    return Response(
        body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/settings/restart", dependencies=[Depends(verify_csrf)])
async def restart(request: Request, db: Session = Depends(get_db)):
    ops = request.app.state.ops
    result = await run_in_threadpool(request_restart, ops.runner)  # sudo may take seconds
    if not result.ok:
        # The reason (sudo output) goes to the log only, never onto the page.
        log.warning("restart failed: %s", (result.stderr or "").strip()[:300])
        return RedirectResponse("/settings?ops=restart_failed#ops-restart", status_code=303)

    new_url = None
    if ops.effective is not None:
        saved = effective_from_settings(db)
        if address_changed(saved, ops.effective):
            new_url = base_url(_display_host(request, saved), saved.port, saved.tls)
    return render(request, "restarting.html", new_url=new_url, active_nav="settings")


def _back(code: str | None = None) -> RedirectResponse:
    query = f"?ops={code}" if code else ""
    return RedirectResponse(f"/settings{query}#ops-update", status_code=303)


@router.post("/settings/update/check", dependencies=[Depends(verify_csrf)])
async def update_check(request: Request):
    app = request.app
    ops = app.state.ops
    if not is_git_install(ops.install_dir):
        return _back("not_git")
    try:
        info = await run_in_threadpool(
            check_versions, ops.install_dir, ops.runner, now=_utc_now()
        )
    except UpdateCheckError as error:
        log.warning("update check failed: %s", str(error)[:300])  # not shown on the page
        return _back("check_failed")
    app.state.version_info = info
    app.state.installed_tag = info.installed
    return _back()


@router.post("/settings/update", dependencies=[Depends(verify_csrf)])
async def update_start(request: Request):
    # No await between the busy check and writing "queued": two clicks cannot both pass.
    app = request.app
    ops = app.state.ops
    if not is_git_install(ops.install_dir):
        return _back("not_git")
    data_dir = _data_dir(request)
    now = _utc_now()
    if update_running(data_dir, ops.runner, now):
        return _back("busy")
    info: VersionInfo | None = getattr(app.state, "version_info", None)
    if info is None or not info.update_available:
        return _back("no_update")
    write_status(
        data_dir,
        UpdateStatus(
            state="queued",
            step="start",
            from_tag=info.installed,
            to_tag=info.newest,
            started_at=now,
        ),
    )
    result = start_update_unit(ops.runner)
    if not result.ok:
        lines = (result.stderr or "").strip().splitlines()
        reason = lines[0][:300] if lines else f"exit code {result.returncode}"
        write_status(
            data_dir,
            UpdateStatus(
                state="failed",
                step="start",
                from_tag=info.installed,
                to_tag=info.newest,
                started_at=now,
                finished_at=_utc_now(),
                message=f"start: {reason}",
            ),
        )
        return _back("start_failed")
    return _back()


@router.get("/settings/update/status")
async def update_status(request: Request):
    return JSONResponse(
        read_status(_data_dir(request)).to_json(), headers={"Cache-Control": "no-store"}
    )
