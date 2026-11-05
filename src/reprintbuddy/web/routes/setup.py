"""First-run setup. Step 1: the admin account. (Step 2 is added in Task 6.)"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy import auth as accounts
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.connection import check_connection
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_db, render, start_session

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


# --- Step 2: connect Bambuddy ------------------------------------------------


def _bambuddy_page(request: Request, error: str | None = None, **values: object):
    values.setdefault("bambuddy_url", "")
    values.setdefault("bambuddy_username", "reprintbuddy")
    values.setdefault("review_confirmed", False)
    # The API key is never put back into the form.
    return render(request, "setup_bambuddy.html", error=error, **values)


@router.get("/setup/bambuddy")
async def bambuddy_form(request: Request, db: Session = Depends(get_db)):
    if st.bambuddy_configured(db):
        return RedirectResponse("/settings", status_code=303)
    return _bambuddy_page(request)


@router.post("/setup/bambuddy")
async def bambuddy_submit(request: Request, db: Session = Depends(get_db)):
    if st.bambuddy_configured(db):
        return RedirectResponse("/settings", status_code=303)
    await verify_csrf(request)

    form = await request.form()
    url_input = str(form.get("bambuddy_url", "")).strip()
    username = str(form.get("bambuddy_username", "")).strip()
    api_key = str(form.get("bambuddy_api_key", ""))
    confirmed = form.get("bambuddy_review_confirmed") is not None
    shown = {"bambuddy_url": url_input, "bambuddy_username": username, "review_confirmed": confirmed}

    if not confirmed:
        return _bambuddy_page(request, "bambuddy.review_required", **shown)
    if not (url_input and username and api_key):
        return _bambuddy_page(request, "bambuddy.fields_required", **shown)
    try:
        url = st.validate("bambuddy_url", url_input)
    except ValueError:
        return _bambuddy_page(request, "bambuddy.invalid_url", **shown)

    async with request.app.state.http_client_factory() as client:
        result = await check_connection(str(url), api_key, client)
    if not result.ok:
        return _bambuddy_page(request, f"bambuddy.reason.{result.reason}", **shown)

    st.set_setting(db, "bambuddy_url", url)
    st.set_setting(db, "bambuddy_username", username)
    st.set_setting(db, "bambuddy_api_key", api_key)
    st.set_setting(db, "bambuddy_review_confirmed", True)
    return RedirectResponse("/", status_code=303)
