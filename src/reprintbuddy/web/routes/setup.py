"""First-run setup. Step 1: the admin account. Steps 2 to 4: connect Bambuddy (three pages)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy import auth as accounts
from reprintbuddy import settings as st
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.bambuddy_check import (
    permission_context,
    result_context,
    run_check,
    validate_address,
    validate_key,
)
from reprintbuddy.web.deps import current_language, flash, get_db, render, start_session, take_verdict

router = APIRouter()


def _require_no_account(db: Session) -> None:
    if accounts.has_user(db):
        raise HTTPException(status_code=404)


@router.get("/setup")
async def setup_form(request: Request, db: Session = Depends(get_db)):
    _require_no_account(db)
    return render(
        request, "setup_admin.html", username="", chosen_language=current_language(request)
    )


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

    # The account and the language are saved together or not at all.
    user = await request.app.state.hash_limiter.run(
        accounts.create_admin, db, username, password, commit=False
    )
    st.set_setting(db, "language", language, commit=False)
    start_session(request, user, db, commit=False)
    db.commit()
    return RedirectResponse("/setup/bambuddy", status_code=303)


# --- Steps 2 to 4: connect Bambuddy (Spec 11.3) -------------------------------

BASE = "/setup/bambuddy"


def _closed(db: Session) -> RedirectResponse | None:
    """Once Bambuddy is connected the pages are closed; the settings page takes over."""
    if st.bambuddy_configured(db):
        return RedirectResponse("/settings?tab=bambuddy", status_code=303)
    return None


@router.get(BASE)
async def bambuddy_start(db: Session = Depends(get_db)):
    return _closed(db) or RedirectResponse(f"{BASE}/user", status_code=303)


# The two guide pages stay readable after the setup: the notice about a missing right
# links to them (Spec 11.3). Then they are a plain guide, without step counter and
# without a way on into the setup; entering the key (step c) stays closed.
@router.get(f"{BASE}/user")
async def bambuddy_user_guide(request: Request, db: Session = Depends(get_db)):
    return render(request, "setup_bambuddy_user.html", guide_only=st.bambuddy_configured(db), **permission_context())


@router.get(f"{BASE}/key")
async def bambuddy_key_guide(request: Request, db: Session = Depends(get_db)):
    return render(request, "setup_bambuddy_key.html", guide_only=st.bambuddy_configured(db), **permission_context())


def _connect_page(request: Request, error: str | None = None, address: str = "", check=None):
    # The API key is never put back into the form.
    return render(
        request,
        "setup_bambuddy_connect.html",
        error=error,
        bambuddy_address=address,
        **result_context(check),
    )


@router.get(f"{BASE}/connect")
async def bambuddy_connect_form(request: Request, db: Session = Depends(get_db)):
    return _closed(db) or _connect_page(request)


@router.post(f"{BASE}/connect")
async def bambuddy_connect_submit(request: Request, db: Session = Depends(get_db)):
    if (closed := _closed(db)) is not None:
        return closed
    await verify_csrf(request)

    form = await request.form()
    address_input = str(form.get("bambuddy_address", "")).strip()
    key_input = str(form.get("bambuddy_token", "")).strip()

    def fail(error: str, check=None):
        return _connect_page(request, error, address_input, check)

    if not (address_input and key_input):
        return fail("bambuddy.fields_required")
    url, error = validate_address(address_input)
    if error:
        return fail(error)
    api_key, error = validate_key(key_input)
    if error:
        return fail(error)

    check = await run_check(request, db, url, api_key)
    if not check.ok:
        return fail(f"bambuddy.reason.{check.reason}", check)

    st.apply_check(db, url, api_key, check)  # one transaction
    take_verdict(request, check)  # this start has checked the new key
    if check.owner_is_admin:
        flash(request, "bambuddy.admin_warning", warning=True)  # saved, but the person is told
        return RedirectResponse("/settings?tab=bambuddy", status_code=303)
    return RedirectResponse("/", status_code=303)
