"""The settings page. One form and one POST route per section.

Sensitive values (Bambuddy API key, TLS private key, passwords) are never
written into a page: the form fields stay empty and an empty field means
"unchanged"; a stored value shows only as the mask.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from reprintbuddy import auth as accounts
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.connection import check_connection
from reprintbuddy.netcheck import can_bind
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import flash, get_db, logged_in_user, render, start_session

router = APIRouter()

MAX_UPLOAD_BYTES = 64 * 1024


def _page(
    request: Request,
    db: Session,
    error: str | None = None,
    section: str | None = None,
    **typed: object,
):
    """Render the page from the stored values; `typed` keeps what was just entered."""
    values: dict[str, object] = {
        "language": st.get_setting(db, "language"),
        "bambuddy_url": st.get_setting(db, "bambuddy_url") or "",
        "bambuddy_username": st.get_setting(db, "bambuddy_username") or "",
        "review_confirmed": st.get_setting(db, "bambuddy_review_confirmed"),
        "host": st.get_setting(db, "host"),
        "port": st.get_setting(db, "port"),
    }
    values.update(typed)
    return render(
        request,
        "settings.html",
        active_nav="settings",
        error=error,
        error_section=section,
        api_key_mask=st.display_value("bambuddy_api_key", st.get_setting(db, "bambuddy_api_key")),
        tls_key_mask=st.display_value("tls_key", st.get_setting(db, "tls_key")),
        tls_cert_set=bool(st.get_setting(db, "tls_cert")),
        **values,
    )


def _saved(request: Request, key: str = "settings.saved") -> RedirectResponse:
    flash(request, key)
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings")
async def settings_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db)


@router.post("/settings/language", dependencies=[Depends(verify_csrf)])
async def save_language(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        st.set_setting(db, "language", str(form.get("language", "")))
    except ValueError:
        return _page(request, db, "settings.invalid_value", "language")
    return _saved(request)


@router.post("/settings/bambuddy", dependencies=[Depends(verify_csrf)])
async def save_bambuddy(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    url_input = str(form.get("bambuddy_url", "")).strip()
    username = str(form.get("bambuddy_username", "")).strip()
    entered_key = str(form.get("bambuddy_api_key", "")).strip()
    confirmed = form.get("bambuddy_review_confirmed") is not None
    typed = {"bambuddy_url": url_input, "bambuddy_username": username, "review_confirmed": confirmed}

    def fail(error: str):
        return _page(request, db, error, "bambuddy", **typed)

    if not confirmed:
        return fail("bambuddy.review_required")
    if not (url_input and username):
        return fail("bambuddy.fields_required")
    try:
        url = st.validate("bambuddy_url", url_input)
    except ValueError:
        return fail("bambuddy.invalid_url")

    if entered_key:
        try:
            entered_key = str(st.validate("bambuddy_api_key", entered_key))
        except ValueError:
            return fail("bambuddy.key_invalid")

    # An empty key field keeps the stored key, but only for the same address:
    # a stored key is never sent to a different scheme, host or port.
    api_key = entered_key
    if not api_key:
        if not st.same_address(str(url), str(st.get_setting(db, "bambuddy_url") or "")):
            return fail("settings.key_required_new_address")
        api_key = str(st.get_setting(db, "bambuddy_api_key") or "")
    if not api_key:
        return fail("bambuddy.fields_required")

    async with request.app.state.http_client_factory() as client:
        result = await check_connection(str(url), api_key, client)
    if not result.ok:
        return fail(f"bambuddy.reason.{result.reason}")

    st.set_setting(db, "bambuddy_url", url)
    st.set_setting(db, "bambuddy_username", username)
    st.set_setting(db, "bambuddy_api_key", api_key)
    st.set_setting(db, "bambuddy_review_confirmed", True)
    return _saved(request)


@router.post("/settings/password", dependencies=[Depends(verify_csrf)])
async def change_password(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    repeat = str(form.get("new_password_repeat", ""))
    user = logged_in_user(request, db)

    def fail(error: str):
        return _page(request, db, error, "password")

    if user is None or accounts.authenticate(db, user.username, current) is None:
        return fail("settings.password_wrong")
    if new != repeat:
        return fail("setup.mismatch")
    if len(new) < accounts.MIN_PASSWORD_LENGTH:
        return fail("setup.too_short")
    accounts.set_password(db, user.username, new)
    # Every other session ends with the old password; this one is issued anew.
    start_session(request, user, db)
    return _saved(request, "settings.password_changed")


async def _pem_input(form, text_field: str, file_field: str) -> str | None:
    """Text or uploaded file for a PEM field; None when left empty.

    Raises ValueError for unreadable or oversized files.
    """
    upload = form.get(file_field)
    if isinstance(upload, UploadFile) and upload.filename:
        raw = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError("file too large")
        return raw.decode("utf-8")  # UnicodeDecodeError is a ValueError
    text = str(form.get(text_field, ""))
    return text if text.strip() else None


@router.post("/settings/server", dependencies=[Depends(verify_csrf)])
async def save_server(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    host = str(form.get("host", "")).strip()
    port_text = str(form.get("port", "")).strip()
    typed = {"host": host, "port": port_text}

    def fail(error: str):
        return _page(request, db, error, "server", **typed)

    try:
        port = st.validate("port", int(port_text))
    except ValueError:
        return fail("settings.invalid_port")
    try:
        host = str(st.validate("host", host))
    except ValueError:
        return fail("settings.invalid_host")
    # An address this machine cannot listen on would lock everybody out after the restart.
    if not await run_in_threadpool(can_bind, host):
        return fail("settings.host_unbindable")

    remove_tls = form.get("tls_remove") is not None
    cert = key = None
    if not remove_tls:
        try:
            cert = await _pem_input(form, "tls_cert", "tls_cert_file")
            key = await _pem_input(form, "tls_key", "tls_key_file")
            # Empty input keeps what is stored.
            cert = st.validate("tls_cert", cert or st.get_setting(db, "tls_cert"))
            key = st.validate("tls_key", key or st.get_setting(db, "tls_key"))
        except ValueError:
            return fail("settings.invalid_pem")  # never echoes the input
        if (cert is None) != (key is None):
            return fail("settings.tls_pair_required")

    st.set_setting(db, "host", host)
    st.set_setting(db, "port", port)
    st.set_setting(db, "tls_cert", cert)
    st.set_setting(db, "tls_key", key)
    return _saved(request, "settings.restart_needed")
