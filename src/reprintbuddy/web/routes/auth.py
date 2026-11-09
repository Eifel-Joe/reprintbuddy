"""Login and logout."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy import auth as accounts
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import (
    SID_KEY,
    get_db,
    logged_in_user,
    release_connection,
    render,
    safe_next,
    start_session,
)

router = APIRouter()


@router.get("/login")
async def login_form(request: Request, next: str | None = None):
    return render(request, "login.html", next=safe_next(next))


async def _wait_for_brake(request: Request, db: Session) -> None:
    brake = request.app.state.login_brake
    pause = brake.current_delay()  # many recent failures: every attempt waits
    if pause:
        release_connection(db)
        await asyncio.sleep(pause)


def _address(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _failed(request: Request, db: Session) -> None:
    request.app.state.login_brake.record_failure()
    # Never the typed name or password (Spec 10.7): the address of the device only.
    request.app.state.activity.record("login_failed", "user", address=_address(request))
    # A short pause per failed attempt slows down guessing; no connection is held meanwhile.
    release_connection(db)
    await asyncio.sleep(request.app.state.login_failure_delay)


async def password_confirmed(request: Request, db: Session, password: str) -> bool:
    """Is `password` the password of the signed-in account? (Spec 11.6: asked again before
    the full backup and the restore, against an open or stolen session.)

    The login's way: the same brake, the same limiter for the hashing, the same pause after
    a failure; no comparison of its own.
    """
    user = logged_in_user(request, db)
    await _wait_for_brake(request, db)
    found = None
    if user is not None:
        found = await request.app.state.hash_limiter.run(accounts.authenticate, db, user.username, password)
    if found is None:
        await _failed(request, db)
        return False
    release_connection(db)
    return True


@router.post("/login", dependencies=[Depends(verify_csrf)])
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    target = safe_next(str(form.get("next") or ""))
    username = str(form.get("username", "")).strip()
    await _wait_for_brake(request, db)
    # Hashing takes a noticeable time and memory: off the event loop, a few at a time.
    user = await request.app.state.hash_limiter.run(
        accounts.authenticate, db, username, str(form.get("password", ""))
    )
    if user is None:
        await _failed(request, db)
        return render(request, "login.html", status_code=200, next=target, error="login.failed")
    start_session(request, user, db)
    request.app.state.activity.record("login", "user", address=_address(request))
    return RedirectResponse(target, status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request, db: Session = Depends(get_db)):
    # Ends this session on the server, so copies of its cookie stop working too;
    # other browsers stay signed in.
    accounts.end_session(db, request.session.get(SID_KEY))
    request.session.clear()
    request.app.state.activity.record("logout", "user", address=_address(request))
    return RedirectResponse("/login", status_code=303)
