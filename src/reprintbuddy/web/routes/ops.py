"""Restart, update and export from the settings page.

Commands run only on a POST with a CSRF token; no GET route starts anything
except the local `git describe` (added with the update section).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy.ops.server import (
    EffectiveServer,
    address_changed,
    base_url,
    connect_host,
    effective_from_settings,
)
from reprintbuddy.ops.service import request_restart
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_db, render

log = logging.getLogger("reprintbuddy.ops")
router = APIRouter()

OPS_MESSAGES = frozenset(
    {"restart_failed", "check_failed", "busy", "no_update", "start_failed", "not_git"}
)


@dataclass
class OpsView:
    running_url: str | None
    restart_pending: bool
    message: str | None  # translation key of a message, or None


def _display_host(request: Request, effective: EffectiveServer) -> str:
    """The wildcard address is no address to open: use the one the browser used."""
    if effective.host in ("0.0.0.0", "::", ""):
        return request.url.hostname or connect_host(effective.host)
    return effective.host


def ops_view(request: Request, session: Session) -> OpsView:
    effective = request.app.state.ops.effective
    running_url = None
    pending = False
    if effective is not None:
        running_url = base_url(_display_host(request, effective), effective.port, effective.tls)
        pending = effective_from_settings(session) != effective
    code = request.query_params.get("ops")
    message = f"ops.msg.{code}" if code in OPS_MESSAGES else None
    return OpsView(running_url=running_url, restart_pending=pending, message=message)


@router.post("/settings/restart", dependencies=[Depends(verify_csrf)])
async def restart(request: Request, db: Session = Depends(get_db)):
    ops = request.app.state.ops
    result = request_restart(ops.runner)
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
