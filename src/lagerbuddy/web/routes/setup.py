"""First-run setup. Step 1: the admin account. (Step 2 is added in Task 6.)"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from lagerbuddy import auth as accounts
from lagerbuddy import settings as st
from lagerbuddy.web.csrf import verify_csrf
from lagerbuddy.web.deps import get_db, render, start_session

router = APIRouter()


def _require_no_account(db: Session) -> None:
    if accounts.has_user(db):
        raise HTTPException(status_code=404)


@router.get("/setup")
async def setup_form(request: Request, db: Session = Depends(get_db)):
    _require_no_account(db)
    return render(request, "setup_admin.html", username="", chosen_language="en")


@router.post("/setup")
async def setup_submit(request: Request, db: Session = Depends(get_db)):
    _require_no_account(db)  # closed forever once an account exists, before anything else
    await verify_csrf(request)

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    repeat = str(form.get("password_repeat", ""))
    language = str(form.get("language", "en"))
    if language not in st.LANGUAGES:
        language = "en"

    error = None
    if not username:
        error = "setup.username_required"
    elif password != repeat:
        error = "setup.mismatch"
    elif len(password) < accounts.MIN_PASSWORD_LENGTH:
        error = "setup.too_short"
    if error:
        # Show the message in the language the person just picked.
        return render(
            request,
            "setup_admin.html",
            error=error,
            username=username,
            chosen_language=language,
            lang=language,
        )

    user = accounts.create_admin(db, username, password)
    st.set_setting(db, "language", language)
    start_session(request, user)
    return RedirectResponse("/setup/bambuddy", status_code=303)
