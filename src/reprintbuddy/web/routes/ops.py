"""Restart, update and export from the settings page.

Commands run only on a POST with a CSRF token. The one command a GET route may run
is `release.json` or the local `git describe` (once per process); GitHub is asked only by the check.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from string import Formatter
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy import __version__
from reprintbuddy import settings as st
from reprintbuddy.activity.kinds import Actor
from reprintbuddy.models import JournalEntry
from reprintbuddy.ops import fullbackup
from reprintbuddy.ops.backup import backup_dir_for, update_backup_since
from reprintbuddy.ops.export import export_bytes
from reprintbuddy.ops.installation import installed_version, is_installation, marker_stamp, read_installation
from reprintbuddy.ops.migrationsteps import StepsError
from reprintbuddy.ops.package import PackageError
from reprintbuddy.ops.releases import ReleaseCheckError, check_versions, fetch_releases, return_target
from reprintbuddy.ops.server import (
    EffectiveServer,
    address_changed,
    base_url,
    connect_host,
    effective_from_settings,
)
from reprintbuddy.ops.running import update_running, update_running_async
from reprintbuddy.ops.runner import CommandResult, in_thread
from reprintbuddy.ops.service import request_restart, start_update_unit
from reprintbuddy.ops.stablereturn import ReturnPlan, assess, prepare_target
from reprintbuddy.ops.status import (
    BUSY_STATES,
    FAILED_STATES,
    UPDATE_REASONS,
    UPDATE_STEPS,
    UpdateStatus,
    is_busy,
    read_status,
    write_status,
)
from reprintbuddy.ops.versions import UpdateCheckError, VersionInfo, is_prerelease_tag
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_db, render
from reprintbuddy.web.flash import flash
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction
from reprintbuddy.web.forms import duplicate_redirect, maintenance_redirect
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.installed import refresh as refresh_installed
from reprintbuddy.web.journal_view import journal_rows
from reprintbuddy.web.routes.auth import password_confirmed

log = logging.getLogger("reprintbuddy.ops")
router = APIRouter()

# Message code from the address -> the card that shows it.
OPS_MESSAGES: dict[str, str] = {
    "restart_failed": "restart",
    **{code: "update" for code in ("check_failed", "busy", "no_update", "start_failed", "not_git")},
    # the way back to the stable version (Spec 11.3a "Rückweg"): why there is none, or why it stopped
    **{
        f"return_{code}": "update"
        for code in ("check_failed", "expired", "none", "no_backup", "backup_unusable", "db_unreadable")
    },
    **{code: "backup" for code in ("backup_mismatch", "backup_length", "backup_failed", "backup_login")},
    **{
        f"restore_{code}": "restore"
        for code in (
            "nofile", "foreign", "too_new", "too_large", "damaged",
            "incompatible", "failed", "expired", "busy",
        )
    },
}

# POST routes that stay usable while an update runs: asking GitHub, the update button
# itself (it answers "busy" on its own), signing out, and the two buttons of the notice
# about a missing right (they ask Bambuddy and set a flag in the cookie, nothing in the
# database). Everything else is paused: the rollback restores the backup made before the
# update, so a booking made meanwhile would be lost.
UPDATE_EXEMPT_PATHS = frozenset(
    {
        "/settings/update/check",
        "/settings/update",
        "/logout",
        "/bambuddy/rights/later",
        "/bambuddy/rights/recheck",
        "/bambuddy/rights/done",
    }
)
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# While a restore replaces the database only signing out passes; when it is replaced but the
# restart failed, also signing in (everybody was signed out) and the restart button.
RESTORE_EXEMPT_PATHS = frozenset({"/logout"})
RESTART_PENDING_EXEMPT_PATHS = frozenset({"/logout", "/login", "/settings/restart"})
# Not counted as writes a restore waits for: they are open without a sign-in (a slow or
# braked login attempt must not hold a restore off) and change no data of the database.
UNCOUNTED_PREFIXES = ("/login", "/logout", "/setup")


@dataclass
class OpsView:
    running_url: str | None
    restart_pending: bool
    message: str | None  # translation key of a message, or None
    message_card: str | None = None  # the card that shows it ("update", "backup", …)
    git_install: bool = False
    installed: str | None = None
    installed_prerelease: bool = False  # v0.4.9: a pre-release runs while only stable ones are offered
    dev_version: str = __version__
    version_info: VersionInfo | None = None
    update: UpdateStatus = UpdateStatus()
    busy: bool = False
    interrupted: bool = False  # the state says "running" but the helper is gone
    not_started: bool = False  # a "queued" start the helper never picked up (p123)
    unrecorded: bool = False  # the helper ran (it backed up) but could not write its state (p145)
    state_params: dict[str, str] = field(default_factory=dict)  # values for the text of `state_key`
    state_key: str | None = None  # translation key for the update state
    step_key: str | None = None  # translation key for the running step
    reason_key: str | None = None  # translation key for why the update failed (p176), if the state file names one
    reason_params: dict[str, str] = field(default_factory=dict)
    reason_open_end: bool = False  # a technical line is empty: the text must not end on its colon (p200)
    dismissable: bool = False  # a failed update's card, which stays until "Hide" (p503)
    return_to: str | None = None  # Spec 11.3a "Rückweg": the stable version a pre-release can go back to


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
    """The tag this process runs: asked once, because every update restarts the process; read again
    when the update writes `release.json` after the restart (p543, `web.installed`)."""
    app = request.app
    ops = app.state.ops
    if not hasattr(app.state, "installed_tag"):
        stamp = marker_stamp(ops.install_dir)  # before the read: a file written in between is read again
        app.state.installed_tag = installed_version(ops.install_dir, ops.runner)
        app.state.installed_stamp = stamp
    refresh_installed(app.state)
    return app.state.installed_tag


async def warm_installed(request: Request) -> None:
    """Ask for the installed tag (`release.json` or `git describe`, once per process) in a worker thread, so the page that
    needs it does not run git in the event loop (v0.3.8 p96, same family)."""
    app = request.app
    if is_installation(app.state.ops.install_dir) and not hasattr(app.state, "installed_tag"):
        await in_thread(_installed, request)


async def update_busy(request: Request) -> bool:
    """Does an update really run? Asked in a worker thread (v0.3.8 p96): systemd may take seconds.

    A click on "Update" whose sudo is still on its way counts as running: "queued" is only believed
    for `QUEUE_GRACE` seconds, and sudo may take as long (review of v0.3.8).
    """
    if request.app.state.update_starting:
        return True
    return await update_running_async(_data_dir(request), request.app.state.ops.runner, _utc_now())


async def ops_ready(request: Request) -> bool:
    """Everything the settings page needs from the system, asked in worker threads: the installed
    version and whether an update runs. Every handler that renders the settings page awaits this and
    hands the answer to `_page` (v0.3.9 p122: the error pages of the saves and the label pages did
    not, and ran `git describe` / `systemctl` in the event loop when they were the first page)."""
    await warm_installed(request)
    return await update_busy(request)


def ops_view(request: Request, session: Session, busy: bool | None = None) -> OpsView:
    """The state of the update card. `busy` is the answer of `update_busy`, asked by the handler that
    can await it (every page of the settings does). Without it the question is asked here, in the
    calling thread: for a caller that cannot await, and in tests."""
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
    view.message_card = OPS_MESSAGES.get(code or "")
    view.dev_version = __version__  # read now, so it is the version of the running code
    view.git_install = is_installation(ops.install_dir)  # (name kept: also a package installation, v0.4.9)
    if view.git_install:
        view.installed = _installed(request)
        view.version_info = getattr(app.state, "version_info", None)
        view.return_to = view.version_info.return_to if view.version_info else None
        view.installed_prerelease = is_prerelease_tag(view.installed or "") and (
            st.get_setting(session, "update_channel") != "prerelease"
        )
    view.update = read_status(_data_dir(request))
    if not request.app.state.restore.active:
        # Uploads nobody confirmed and downloads that were cut off hold every secret.
        fullbackup.clear_work_dir(fullbackup.work_dir_for(app.state.db_path), older_than=fullbackup.STALE_AFTER)
    view.busy = (
        busy if busy is not None else update_running(_data_dir(request), request.app.state.ops.runner, _utc_now())
    )
    view.interrupted = view.update.state in BUSY_STATES and not view.busy
    if view.interrupted and view.update.state == "queued":
        _queued_left_behind(view, app.state.db_path)
    elif view.interrupted:
        view.state_key = "ops.update.state.interrupted"
    elif view.update.state != "idle" and not view.update.acknowledged:
        view.state_key = f"ops.update.state.{view.update.state}"
        if view.update.kind == "return":
            # Spec 11.3a "Rückweg": in its own words ("Rückweg fehlgeschlagen, Vorabversion läuft weiter"); after
            # a success the stable version says the switch "include pre-releases" is now off.
            view.state_key = f"ops.return.state.{view.update.state}"
            view.state_params = {"version": (view.update.to_tag or "")[1:]}
        view.dismissable = view.update.state in FAILED_STATES
    if (view.busy or view.interrupted) and view.update.step in UPDATE_STEPS:
        view.step_key = f"ops.update.step.{view.update.step}"
    view.reason_key, view.reason_params = _reason_text(view.update, str(st.get_setting(session, "language")))
    view.reason_open_end = any(not value.strip() for value in view.reason_params.values())
    return view


def mark_result_seen(request: Request, view: OpsView) -> None:
    """A successful update's card is shown once (Spec 11.3a, p503): the render that shows it also
    records that it was shown, so the next call of the page no longer has it. A failed update's card is
    not touched here; it stays until the user hides it (`update_dismiss`). The caller calls this only
    for a render of the tab that holds the card. A file that cannot be written is no reason to fail the
    page: the card then shows once more."""
    status = view.update
    if not (view.git_install and view.state_key and status.state == "succeeded" and not status.acknowledged):
        return
    try:
        write_status(_data_dir(request), replace(status, acknowledged=True))
    except OSError:
        log.warning("update: could not record that the result was shown", exc_info=True)


def _reason_text(status: UpdateStatus, lang: str = "en") -> tuple[str | None, dict[str, str]]:
    """The text of why an update failed, in the language of the viewer (v0.3.14 p176).

    The helper writes a code and its values next to the English line (`UpdateStatus.reason`, `params`).
    A state file without a code (written by an older version, or by hand), with a code this version does
    not know or with values the text needs and the file does not have gives (None, {}); the card then shows
    the English line as before. `detail` is a technical line of git, pip or the system and stays as it is.

    An empty value (a state file written by hand): the technical line at the very end of a text stays empty,
    the card cuts the colon in front of it (p200). A value in the middle of a sentence ("found {seen}.")
    shows a word for "empty" instead, so no gap or lone full stop is left (v0.3.16 p250).
    """
    if status.reason not in UPDATE_REASONS:
        return None, {}
    key = f"ops.update.reason.{status.reason}"
    needed = {name for _, name, _, _ in Formatter().parse(translate("en", key)) if name}
    if not needed <= set(status.params):
        return None, {}
    parsed = list(Formatter().parse(translate(lang, key)))
    last = parsed[-1][1] if parsed else None  # the field the text ends on, if it does
    params = {}
    for name in needed:
        value = status.params[name]
        if not value.strip() and name != last:
            value = translate(lang, "ops.update.empty_value")
        params[name] = value
    return key, params


def _queued_left_behind(view: OpsView, db_path) -> None:
    """The card for a "queued" whose grace is over and whose unit does not run.

    v0.3.11 p123: a start cut off by a shutdown. The helper's first act is to overwrite "queued" with
    "running" (`updater.run_update`); a "queued" left behind was never picked up, so "interrupted" would
    name a run that never began. Only the card says so; the file stays, a later start overwrites it.

    v0.3.12 p145: a helper that cannot write its state file at all (a read-only file, say) also leaves
    "queued" behind, after it backed up the database, changed things and maybe rolled them back. The
    backup it made after the start tells the two apart (`update_backup_since`): with one, the card points
    to the helper's log, whichever version runs, and never says "finished" or "did not start". Without
    one the helper changed nothing: it never began, or it began and already the backup failed (a full
    disk); "did not start" is right for both. "Finished" only when the target version runs and no backup
    was made since the start (not reached by today's helper, which always backs up first).

    v0.3.12 p144: "please start it again" needs a way to: the update button appears only after a check.
    The page does not ask GitHub (the check runs on a button press, or at the start and daily when it is
    switched on, Spec 11.3a). Without a check since the start the card asks for one and the template puts
    the check button at the notice; when the check found nothing newer (a withdrawn version), it says so.
    Tests: test_ops_update.py "p123", "p144", "p145".
    """
    view.interrupted = False
    status = view.update
    target_runs = bool(view.installed) and view.installed == status.to_tag
    backed_up = status.started_at is not None and update_backup_since(
        backup_dir_for(db_path), status.started_at, status.from_tag
    )
    if backed_up:
        view.unrecorded = True
        if target_runs:
            view.state_key = "ops.update.state.unrecorded_target"
            view.state_params = {"version": status.to_tag or ""}
        else:
            view.state_key = "ops.update.state.unrecorded"
        return
    if target_runs:
        view.state_key = "ops.update.state.succeeded"
        return
    view.not_started = True
    info = view.version_info
    if info is None:
        view.state_key = "ops.update.state.not_started_check"
    elif info.update_available:
        view.state_key = "ops.update.state.not_started"
    else:
        view.state_key = "ops.update.state.not_started_none"
        view.state_params = {"version": status.to_tag or ""}


def restore_blocks(request: Request) -> bool:
    """Does a restore pause this write (see `RESTORE_EXEMPT_PATHS`)?"""
    state = getattr(request.app.state, "restore", None)
    if state is None or not state.active:
        return False
    allowed = RESTART_PENDING_EXEMPT_PATHS if state.phase == "restart_pending" else RESTORE_EXEMPT_PATHS
    return request.url.path not in allowed


def _restore_pause(request: Request) -> Response | None:
    """The 503 page of a write that a running restore pauses, else None."""
    if not restore_blocks(request):
        return None
    state = request.app.state.restore
    return render(
        request,
        "error.html",
        status_code=503,
        layout="base.html",
        title_key="error.generic.title",
        message_key="ops.restore.restart_pending" if state.phase == "restart_pending" else "ops.restore.in_progress",
    )


async def update_guard(request: Request, call_next) -> Response:
    """Pause every write while an update or a restore runs (see `UPDATE_EXEMPT_PATHS`).

    The writes it lets through are counted while they run: a restore waits for the ones
    that passed just before it began.

    The question whether an update runs is asked in a worker thread (v0.3.8 p96), and a restore may
    begin while this request waits for the answer. So the restore is looked at again after the
    answer, and the request is counted in the same step, with no `await` between: a write is either
    refused or counted before a restore that began meanwhile looks at the count.
    """
    is_write = request.method in WRITE_METHODS
    if is_write:
        paused = _restore_pause(request)
        if paused is not None:
            return paused
    if is_write and request.url.path not in UPDATE_EXEMPT_PATHS:
        if await update_busy(request):
            return render(
                request,
                "error.html",
                status_code=503,
                layout="base.html",
                title_key="error.generic.title",
                message_key="ops.update.in_progress",
            )
        paused = _restore_pause(request)  # a restore that began while the question was out
        if paused is not None:
            return paused
    if not is_write or request.url.path.startswith(UNCOUNTED_PREFIXES):
        return await call_next(request)
    state = request.app.state
    state.writes_in_flight += 1
    try:
        return await call_next(request)
    finally:
        state.writes_in_flight -= 1


@router.get("/settings/export")
def export(request: Request, db: Session = Depends(get_db)):
    # A plain `def`: reading every table is blocking work and runs in the thread pool.
    now = _utc_now()
    body = export_bytes(db.connection(), now=now)
    request.app.state.activity.record("export_downloaded", "user")
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
    result = await in_thread(request_restart, ops.runner)  # sudo may take seconds; a shutdown stops it
    if not result.ok:
        # The reason (sudo output) goes to the log only, never onto the page.
        log.warning("restart failed: %s", (result.stderr or "").strip()[:300])
        return RedirectResponse("/settings?tab=general&ops=restart_failed#ops-restart", status_code=303)
    request.app.state.activity.record("restart_requested", "user")

    new_url = None
    if ops.effective is not None:
        saved = effective_from_settings(db)
        if address_changed(saved, ops.effective):
            new_url = base_url(_display_host(request, saved), saved.port, saved.tls)
    return render(request, "restarting.html", new_url=new_url, active_nav="settings")


def store_versions(state: Any, info: VersionInfo, *, actor: Actor) -> None:
    """Keep a good result of the check for new versions, from the button or the automatic check (one
    state), and log a newer version once (Spec 10.7, group 4).

    p543: the check read the installation itself, at a time not known here; when `release.json` names
    another version by now (the update wrote it while the check ran), the result is dropped, and nothing is
    logged for it."""
    state.version_info = info
    state.installed_tag = info.installed
    state.installed_stamp = None  # compared with `release.json` at once
    refresh_installed(state)
    if state.version_info is not info:
        return
    if info.update_available and info.newest:
        state.activity.transition("update.available", info.newest, "version_found", actor, {"version": info.newest})


def _back(code: str | None = None) -> RedirectResponse:
    return maintenance_redirect(code, "ops-update")


@router.post("/settings/update/check", dependencies=[Depends(verify_csrf)])
async def update_check(request: Request):
    app = request.app
    ops = app.state.ops
    if not is_installation(ops.install_dir):
        return _back("not_git")
    return await _check(request)


async def _check(request: Request):
    """The check for new versions, for the button and for a new choice of versions (Spec 11.3a)."""
    app = request.app
    ops = app.state.ops
    with app.state.sessionmaker() as session:
        channel = st.get_setting(session, "update_channel")
    try:
        # v0.4.9: one request to GitHub's release list gives versions and notes (Spec 11.3a); in a thread.
        info = await in_thread(check_versions, ops.install_dir, ops.runner, ops.http_get, channel=channel, now=_utc_now())
    except UpdateCheckError as error:
        log.warning("update check failed: %s", str(error)[:300])  # not shown on the page
        return _back("check_failed")
    store_versions(app.state, info, actor="user")
    return _back()


@router.post("/settings/update/channel", dependencies=[Depends(verify_csrf)])
async def update_channel(request: Request):
    """The switch "Include pre-releases" (v0.4.9, Spec 11.3a "Vorabversionen"; a switch since p542, v0.4.10):
    `enabled=1` is the wanted state "pre-releases too", `enabled=0` "stable releases only", never a toggle
    (two open tabs cannot turn each other back). Saved, then checked at once with the new choice, also when
    the automatic check is off ("wirkt sofort und löst eine neue Prüfung aus"). A value that is neither
    changes nothing and asks nobody; a form sent again or an expired one changes nothing, as on the switch of
    the automatic check above it (p99)."""
    form = await request.form()
    wanted = {"1": "prerelease", "0": "stable"}.get(str(form.get("enabled", "")).strip())
    if wanted is None:
        return _back()
    try:
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            st.set_setting(tx, "update_channel", wanted, commit=False)
    except FormTokenUsed:
        return duplicate_redirect(request, _back().headers["location"])
    except FormTokenInvalid:
        flash(request, "form.expired", warning=True)
        return _back()
    if not is_installation(request.app.state.ops.install_dir):
        return _back()
    # Final review: what the earlier check found was offered under the earlier choice. Forgotten here, so
    # that a check that fails now leaves no pre-release on offer (bar, button) after "stable releases only".
    request.app.state.version_info = None
    return await _check(request)


@router.post("/settings/update/auto", dependencies=[Depends(verify_csrf)])
async def update_auto(request: Request):
    """The switch of the automatic check (Spec 11.3a): `enabled=1|0` is the wanted state, never a
    toggle (two open tabs cannot turn each other back). A value that is neither changes nothing.
    Ends the pause of the check loop, so a check that was switched on runs now.

    v0.3.13 p99: the one-time token is used up like on every other form. Setting the same state twice
    did no harm, but a form sent again from an old tab set back what another tab had changed since.
    A repeat or an expired token changes nothing and does not wake the loop. Tests: test_update_banner.py "p99".
    """
    form = await request.form()
    wanted = str(form.get("enabled", "")).strip()
    if wanted not in ("0", "1"):
        return _back()
    try:
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            st.set_setting(tx, "update_check", wanted == "1", commit=False)
    except FormTokenUsed:
        return duplicate_redirect(request, _back().headers["location"])
    except FormTokenInvalid:
        flash(request, "form.expired", warning=True)
        return _back()
    request.app.state.update_check_wake.set()
    return _back()


@router.post("/settings/update", dependencies=[Depends(verify_csrf)])
async def update_start(request: Request):
    """Start the update helper. Nothing here blocks the event loop (v0.3.8 p96): the questions to
    systemd and the start of the unit through sudo (up to 15 s) run in worker threads.

    What keeps two clicks, or a click and a restore, from both getting through: `update_starting` is
    set with no `await` between looking at it and setting it; the restore state is looked at again
    right before "queued" is written, with no `await` between those two either. A restore sets its own
    state before it asks about updates, so one of the two sees the other.
    """
    return await _gated_start(request)


async def _gated_start(request: Request, way_back: ReturnPlan | None = None):
    """The gate of `update_start`, for an update and for the way back to the stable version (Spec 11.3a)."""
    app = request.app
    ops = app.state.ops
    if not is_installation(ops.install_dir):
        return _back("not_git")
    # A restore that is replacing the database (or waits for its restart) counts as busy, and so does
    # another click that is still on its way.
    if app.state.restore.active or app.state.update_starting:
        return _back("busy")
    app.state.update_starting = True
    # p310: in the same step as the gate, with no `await` in between, a push message on its way may
    # still write its outcome (as for the restore, p270); `_start_update` waits for it before the
    # helper starts, every way out closes the window again.
    app.state.notifier.begin_settle()
    try:
        return await _start_update(request, way_back)
    finally:
        app.state.notifier.end_settle()
        app.state.update_starting = False


async def _start_update(request: Request, way_back: ReturnPlan | None = None):
    app = request.app
    ops = app.state.ops
    data_dir = _data_dir(request)
    now = _utc_now()
    # The state file alone says "idle" for most clicks; only a recorded update needs systemd's word.
    if is_busy(read_status(data_dir), now) and await update_running_async(data_dir, ops.runner, now):
        return _back("busy")
    refresh_installed(app.state)  # p543: not an update to the version the last update installed
    info: VersionInfo | None = getattr(app.state, "version_info", None)
    if way_back is None and (info is None or not info.update_available):
        return _back("no_update")
    if way_back is not None and (info is None or info.return_to != way_back.target):
        return _back("return_expired")  # the check since then names another stable version, or none
    # p310: Home Assistant could accept a message while the update began; the notifier then refused to
    # write "sent" (an update had started), and the new version sent it once more after its restart.
    # The helper's backup and restart come only after the outcome of that call is written. Bounded:
    # past it the update goes ahead and the message may come once more. Rejected: letting the notifier
    # write while the helper runs - the helper migrates the database, writes must have stopped by then.
    if not await app.state.notifier.settle(app.state.update_notify_wait):
        log.info("update: a push message was still on its way; it may be sent once more")
    # From the last look at the restore to "queued" there is no `await`. `started_at` is taken here,
    # not before the questions above: the grace in which "queued" counts as running starts now.
    if app.state.restore.active:
        return _back("busy")
    now = _utc_now()
    queued = UpdateStatus(state="queued", step="start", from_tag=info.installed, to_tag=info.newest, started_at=now)
    if way_back is not None:
        # Spec 11.3a "Rückweg": what the user confirmed; the helper decides again and changes nothing if it differs.
        queued = replace(
            queued,
            to_tag=way_back.target,
            kind="return",
            return_case=way_back.case,
            target_head=way_back.target_head,
            return_backup=str(way_back.backup) if way_back.case == 3 and way_back.backup else None,
        )
    write_status(data_dir, queued)
    # Cancelled here (a shutdown): sudo is killed, the gate is freed and "queued" stays; it is believed
    # for the grace only, then systemd's word counts, and the card says the update did not start (or,
    # when the target version runs, that it finished: `ops_view`, p123). Not rewritten to "failed":
    # `--no-block` may have gone through, and the helper then overwrites "queued" itself.
    try:
        result = await in_thread(start_update_unit, ops.runner)  # sudo may take seconds
    except Exception as error:  # noqa: BLE001 - the state must not stay "queued" for a unit that never started
        result = CommandResult(1, "", type(error).__name__)
    if not result.ok:
        lines = (result.stderr or "").strip().splitlines()
        reason = lines[0][:300] if lines else f"exit code {result.returncode}"
        write_status(
            data_dir,
            replace(
                queued,
                state="failed",
                finished_at=_utc_now(),
                message=f"start: {reason}",
                reason="start_failed",
                params={"detail": reason},
            ),
        )
        return _back("start_failed")
    return _back()


# --- the way back to the stable version (Spec 11.3a "Rückweg zur stabilen Version") -----------------------

# (Festlegung Claude, confirmed with the plan) how long the result of the check counts for the confirmation:
# long enough to read the page and download a backup, short enough that it still describes the database.
RETURN_FRESH = timedelta(minutes=30)
# Up to this many journal bookings the list on the confirmation page is open; more, and it starts closed with
# the number in its summary (Spec 11.3a case 3, F5; review focus 5). The list has no limit.
RETURN_OPEN_UP_TO = 10
RETURN_CHECK_DIR = ".return-check"  # beside the database while a click checks (package, copies); gone after


class ReturnTargetChanged(ReleaseCheckError):
    """GitHub now names another stable version to go back to than the card shows: check for updates first."""
MAX_LOGIN_FIELD = 4 * 1024


def _prepare_return(app: Any, target: str) -> ReturnPlan:
    """In a worker thread: the release list, the checked package of `target`, the case (`stablereturn.assess`)."""
    ops = app.state.ops
    found = read_installation(ops.install_dir, ops.runner)
    if found is None or found.repo is None or found.tag is None:
        raise ReleaseCheckError("no installation")
    release = return_target(fetch_releases(found.repo, ops.http_get), found.tag)
    if release is None or release.tag != target:
        raise ReturnTargetChanged("the stable version to go back to has changed")
    db_path = app.state.db_path
    # A folder of its own per check (review: with one shared folder a double click let each check delete the
    # files of the other).
    work = Path(tempfile.mkdtemp(prefix=RETURN_CHECK_DIR + "-", dir=db_path.parent))
    try:
        graph = prepare_target(release, ops.download, work / "package")
        return assess(
            db_path, target=release.tag, target_graph=graph, backup_dir=backup_dir_for(db_path), work_dir=work,
            script_location=ops.script_location,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _prepared(request: Request) -> ReturnPlan | None:
    held = getattr(request.app.state, "return_prepared", None)
    if not held:
        return None
    plan, at = held
    return plan if _utc_now() - at <= RETURN_FRESH else None


@router.post("/settings/update/return/check", dependencies=[Depends(verify_csrf)])
async def return_check(request: Request):
    """The button "Zurück zur stabilen Version X.Y.Z": load and check the stable package, decide the case and
    rehearse it on a copy, then show the confirmation page. Like "Check for updates", this asks the network;
    the page itself never does. Without a way back the card says why."""
    app = request.app
    ops = app.state.ops
    if not is_installation(ops.install_dir):
        return _back("not_git")
    if app.state.restore.active or app.state.update_starting:
        return _back("busy")
    data_dir = _data_dir(request)
    now = _utc_now()
    if is_busy(read_status(data_dir), now) and await update_running_async(data_dir, ops.runner, now):
        return _back("busy")
    refresh_installed(app.state)
    info: VersionInfo | None = getattr(app.state, "version_info", None)
    if info is None or not info.return_to:
        return _back("return_none")
    app.state.return_prepared = None
    try:
        plan = await in_thread(_prepare_return, app, info.return_to)
    except ReturnTargetChanged:
        return _back("return_none")  # the card's version is out of date: "check for updates first"
    except (UpdateCheckError, PackageError, StepsError, OSError, sqlite3.Error) as error:
        log.warning("way back: the check failed: %s", str(error)[:300])  # not shown on the page
        return _back("return_check_failed")
    if plan.case is None:
        return _back(f"return_{plan.unavailable}")
    app.state.return_prepared = (plan, _utc_now())
    return RedirectResponse("/settings/update/return", status_code=303)


# p611: the messages of the backup form that the confirmation page shows in its card "Backup" (`backup._backup_error`).
RETURN_BACKUP_MESSAGES = frozenset({"backup_mismatch", "backup_length", "backup_login", "backup_failed"})


def _return_page(
    request: Request, db: Session, plan: ReturnPlan, *, error: str | None = None, backup_code: str | None = None
) -> Response:
    rows: list[dict[str, Any]] = []
    if plan.case == 3 and plan.journal_ids:
        wanted = set(plan.journal_ids)
        entries = db.scalars(
            select(JournalEntry).where(JournalEntry.id >= min(wanted)).order_by(JournalEntry.id.desc())
        ).all()
        rows = journal_rows(db, [entry for entry in entries if entry.id in wanted])
    lang = "de" if str(st.get_setting(db, "language")) == "de" else "en"
    return render(
        request,
        "return_confirm.html",
        status_code=400 if error else 200,
        plan=plan,
        installed=getattr(request.app.state, "installed_tag", None),
        rows=rows,
        list_open=len(rows) <= RETURN_OPEN_UP_TO,
        lang=lang,
        error=error,
        backup_message=f"ops.msg.{backup_code}" if backup_code in RETURN_BACKUP_MESSAGES else None,
    )


@router.get("/settings/update/return")
async def return_page(request: Request, db: Session = Depends(get_db)):
    """The confirmation page (Spec 11.3a): the case, what is lost, the backup to download. No network."""
    plan = _prepared(request)
    if plan is None:
        return _back("return_expired")
    return _return_page(request, db, plan, backup_code=request.query_params.get("ops"))


@router.post("/settings/update/return", dependencies=[Depends(verify_csrf)])
async def return_confirm(request: Request, db: Session = Depends(get_db)):
    """Start the way back the page described. Case 3 asks the password again, as a restore does (Spec 11.6);
    cases 1 and 2 keep the data, a click is enough. The case the page showed must be the one checked."""
    form = await request.form()
    plan = _prepared(request)
    if plan is None or str(form.get("case") or "") != str(plan.case):
        return _back("return_expired")
    if plan.case == 3:
        login = str(form.get("login_password") or "")[:MAX_LOGIN_FIELD]
        if not await password_confirmed(request, db, login):
            return _return_page(request, db, plan, error="ops.return.password_wrong")
    response = await _gated_start(request, plan)
    if "ops=" not in response.headers.get("location", ""):
        request.app.state.return_prepared = None  # started: the check is used up
    return response


@router.post("/settings/update/dismiss", dependencies=[Depends(verify_csrf)])
async def update_dismiss(request: Request):
    """Hide the card of a failed or rolled-back update (Spec 11.3a, p503). The record stays in the
    state file and in the log; a running or successful update is left alone."""
    data_dir = _data_dir(request)
    status = read_status(data_dir)
    if status.state in FAILED_STATES and not status.acknowledged:
        write_status(data_dir, replace(status, acknowledged=True))
    return _back()


@router.get("/settings/update/status")
async def update_status(request: Request):
    return JSONResponse(
        read_status(_data_dir(request)).to_json(), headers={"Cache-Control": "no-store"}
    )
