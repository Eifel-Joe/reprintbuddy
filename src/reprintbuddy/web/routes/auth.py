"""Login and logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy import auth as accounts
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_db, render, safe_next, start_session

router = APIRouter()


@router.get("/login")
async def login_form(request: Request, next: str | None = None):
    return render(request, "login.html", next=safe_next(next))


@router.post("/login", dependencies=[Depends(verify_csrf)])
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    target = safe_next(str(form.get("next") or ""))
    user = accounts.authenticate(db, str(form.get("username", "")), str(form.get("password", "")))
    if user is None:
        return render(request, "login.html", status_code=200, next=target, error="login.failed")
    start_session(request, user)
    return RedirectResponse(target, status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
