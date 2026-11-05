"""Login and logout."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from reprintbuddy import auth as accounts
from reprintbuddy import settings as st
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_db, release_connection, render, safe_next, start_session

router = APIRouter()


@router.get("/login")
async def login_form(request: Request, next: str | None = None):
    return render(request, "login.html", next=safe_next(next))


@router.post("/login", dependencies=[Depends(verify_csrf)])
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    target = safe_next(str(form.get("next") or ""))
    username = str(form.get("username", "")).strip()
    # Hashing takes a noticeable time: keep it off the event loop.
    user = await run_in_threadpool(accounts.authenticate, db, username, str(form.get("password", "")))
    if user is None:
        # A short pause per failed attempt slows down guessing; no connection is held meanwhile.
        release_connection(db)
        await asyncio.sleep(request.app.state.login_failure_delay)
        return render(request, "login.html", status_code=200, next=target, error="login.failed")
    start_session(request, user, db)
    return RedirectResponse(target, status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request, db: Session = Depends(get_db)):
    # Raising the generation also ends copies of this session's cookie.
    st.bump_session_generation(db)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
