"""The settings page. One form and one POST route per section.

Sensitive values (Bambuddy API key, TLS private key, passwords) are never
written into a page: the form fields stay empty and an empty field means
"unchanged"; a stored value shows only as the mask.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from reprintbuddy import auth as accounts
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.live import NO_RIGHT_DELAY
from reprintbuddy.netcheck import can_bind
from reprintbuddy.ops.tls import validate_tls_pair
from reprintbuddy.web import qr as qr_view
from reprintbuddy.web.bambuddy_check import permission_context, run_check, validate_address, validate_key
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.routes.auth import password_confirmed
from reprintbuddy.web.routes.ops import ops_view
from reprintbuddy.web.deps import flash, get_db, logged_in_user, render, start_session, take_verdict
from reprintbuddy.web.formtoken import write_transaction

router = APIRouter()

MAX_UPLOAD_BYTES = 64 * 1024

# Tabs of the page: (key, icon, label key). Another feature adds its tab as a line at the agreed
# position (Automation sits second, p43) and renders its cards when `current_tab` is its key.
SETTINGS_TABS: list[tuple[str, str, str]] = [
    ("general", "settings", "settings.tab.general"),
    ("automation", "list-ordered", "settings.tab.automation"),
    ("bambuddy", "link", "settings.tab.bambuddy"),
    ("account", "user", "settings.tab.account"),
    ("maintenance", "download", "settings.tab.maintenance"),
]
DEFAULT_TAB = "general"
# The tab that shows the card of a section: a page re-rendered with an error opens it.
SECTION_TAB = {
    "language": "general",
    "server": "general",
    "qr": "general",
    "bambuddy": "bambuddy",
    "password": "account",
    "automation": "automation",
}


def _tab_from_query(request: Request) -> str:
    key = request.query_params.get("tab", DEFAULT_TAB)
    return key if key in {tab[0] for tab in SETTINGS_TABS} else DEFAULT_TAB


def _page(
    request: Request,
    db: Session,
    error: str | None = None,
    section: str | None = None,
    check=None,
    **typed: object,
):
    """Render the page from the stored values; `typed` keeps what was just entered."""
    values: dict[str, object] = {
        "language": st.get_setting(db, "language"),
        "bambuddy_address": st.get_setting(db, "bambuddy_url") or "",
        "bambuddy_owner": st.get_setting(db, "bambuddy_username") or "",
        "qr_base_url": st.get_setting(db, "qr_base_url") or "",
        "qr_automatic": qr_view.automatic_base(request),
        "host": st.get_setting(db, "host"),
        "port": st.get_setting(db, "port"),
        "automation": st.get_setting(db, "automation"),
        "queue_stopped": not st.get_setting(db, "queue_running"),
        "queue_cap": st.get_setting(db, "queue_cap"),
        "insert_next": st.get_setting(db, "insert_next"),
        "poll_minutes": st.get_setting(db, "poll_minutes"),
    }
    values.update(typed)
    current_tab = SECTION_TAB.get(section or "") or _tab_from_query(request)
    return render(
        request,
        "settings.html",
        active_nav="settings",
        error=error,
        error_section=section,
        settings_tabs=SETTINGS_TABS,
        current_tab=current_tab,
        api_key_mask=st.display_value("bambuddy_api_key", st.get_setting(db, "bambuddy_api_key")),
        tls_key_mask=st.display_value("tls_key", st.get_setting(db, "tls_key")),
        tls_cert_set=bool(st.get_setting(db, "tls_cert")),
        ops=ops_view(request, db),
        update_check=bool(st.get_setting(db, "update_check")),  # the switch of the automatic check
        # The live channel row: only with a stored address (without one there is nothing to connect to).
        # The stored setting decides, not `values`: a typed address that was refused is no address.
        live_channel=request.app.state.live_listener.state if st.get_setting(db, "bambuddy_url") else None,
        channel_retry_minutes=NO_RIGHT_DELAY // 60,
        **permission_context(),
        check=check,
        **values,
    )


def _saved(
    request: Request, key: str = "settings.saved", *, warning: bool = False, tab: str = DEFAULT_TAB
) -> RedirectResponse:
    flash(request, key, warning=warning)
    return RedirectResponse(f"/settings?tab={tab}", status_code=303)


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
    return _saved(request, tab=SECTION_TAB["language"])


@router.post("/settings/qr", dependencies=[Depends(verify_csrf)])
async def save_qr(request: Request, db: Session = Depends(get_db)):
    """The address inside the QR codes (Spec 8.4). Empty removes it: the codes then carry the
    address Reprintbuddy is called on."""
    form = await request.form()
    typed = str(form.get("qr_base_url", "")).strip()
    try:
        st.set_setting(db, "qr_base_url", typed or None)
    except ValueError:
        return _page(request, db, "settings.invalid_qr_base_url", "qr", qr_base_url=typed)
    return _saved(request, tab=SECTION_TAB["qr"])


@router.post("/settings/bambuddy", dependencies=[Depends(verify_csrf)])
async def save_bambuddy(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    address_input = str(form.get("bambuddy_address", "")).strip()
    entered_key = str(form.get("bambuddy_token", "")).strip()
    typed = {"bambuddy_address": address_input}

    def fail(error: str, check=None):
        return _page(request, db, error, "bambuddy", check, **typed)

    if not address_input:
        return fail("bambuddy.fields_required")
    url, error = validate_address(address_input)
    if error:
        return fail(error)

    if entered_key:
        entered_key, error = validate_key(entered_key)
        if error:
            return fail(error)

    # An empty key field keeps the stored key, but only for the same address:
    # a stored key is never sent to a different scheme, host or port.
    api_key = entered_key
    if not api_key:
        if not st.same_address(str(url), str(st.get_setting(db, "bambuddy_url") or "")):
            return fail("settings.key_required_new_address")
        api_key = str(st.get_setting(db, "bambuddy_api_key") or "")
    if not api_key:
        return fail("bambuddy.fields_required")

    check = await run_check(request, db, url, api_key)
    if not check.ok:
        return fail(f"bambuddy.reason.{check.reason}", check)

    # A standing connection belongs to the old address and key: only a change replaces it.
    stored_url = str(st.get_setting(db, "bambuddy_url") or "")
    changed = not (
        st.same_address(str(url), stored_url)
        and urlsplit(str(url)).path.rstrip("/") == urlsplit(stored_url).path.rstrip("/")  # a path prefix counts too
        and api_key == str(st.get_setting(db, "bambuddy_api_key") or "")
    )
    # One transaction: a failure must not leave a new address with the old key.
    st.apply_check(db, url, api_key, check)
    # p97: a new key or new rights must not wait for the live channel's pause (up to 15 minutes).
    request.app.state.live_listener.wake(reconnect=changed)
    take_verdict(request, check)  # a fixed key clears the notice at once
    if check.owner_is_admin:
        return _saved(request, "bambuddy.admin_warning", warning=True, tab=SECTION_TAB["bambuddy"])
    return _saved(request, tab=SECTION_TAB["bambuddy"])


@router.post("/settings/password", dependencies=[Depends(verify_csrf)])
async def change_password(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    repeat = str(form.get("new_password_repeat", ""))
    user = logged_in_user(request, db)

    def fail(error: str):
        return _page(request, db, error, "password")

    limiter = request.app.state.hash_limiter
    # The login's way, brake and pause included: guessing the current password here must
    # cost what a login costs (a stolen session would otherwise guess it unbraked).
    if user is None or not await password_confirmed(request, db, current):
        return fail("settings.password_wrong")
    if new != repeat:
        return fail("setup.mismatch")
    if len(new) < accounts.MIN_PASSWORD_LENGTH:
        return fail("setup.too_short")
    await limiter.run(accounts.set_password, db, user.username, new)
    # Every other session ends with the old password; this one is issued anew.
    start_session(request, user, db)
    return _saved(request, "settings.password_changed", tab=SECTION_TAB["password"])


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
            return fail("ops.tls.incomplete")
        if cert is not None and key is not None:
            # A pair that does not load would keep Reprintbuddy from starting after the restart.
            try:
                await run_in_threadpool(validate_tls_pair, str(cert), str(key))
            except ValueError as problem:
                return fail("ops.tls.encrypted" if str(problem) == "tls_encrypted" else "ops.tls.invalid")

    # One transaction: no half a TLS pair, no new address with an old port.
    st.set_setting(db, "host", host, commit=False)
    st.set_setting(db, "port", port, commit=False)
    st.set_setting(db, "tls_cert", cert, commit=False)
    st.set_setting(db, "tls_key", key, commit=False)
    db.commit()
    # Ports below 1024 need CAP_NET_BIND_SERVICE: the installed service has it, others may not.
    if port < 1024:
        return _saved(request, "settings.privileged_port", warning=True, tab=SECTION_TAB["server"])
    return _saved(request, "settings.restart_needed", tab=SECTION_TAB["server"])


@router.post("/settings/automation", dependencies=[Depends(verify_csrf)])
async def save_automation(request: Request, db: Session = Depends(get_db)):
    """Main switch, stop the queue, limit, insert as next, poll interval (Spec 7.3, 7.6, 7.7).

    An unticked box is not sent at all: missing means off. A wrong number saves nothing.
    """
    form = await request.form()
    flags = {name: form.get(name) is not None for name in ("automation", "queue_stopped", "insert_next")}
    numbers = {name: str(form.get(name, "")).strip() for name in ("queue_cap", "poll_minutes")}
    checked: dict[str, int] = {}
    for name, text in numbers.items():
        try:
            checked[name] = int(st.validate(name, int(text)))
        except ValueError:  # also not a number at all
            return _page(request, db, f"settings.invalid_{name}", "automation", **flags, **numbers)

    old_poll_minutes = int(st.get_setting(db, "poll_minutes"))
    # One transaction: all five or none.
    with write_transaction(request) as session:
        st.set_setting(session, "automation", flags["automation"], commit=False)
        st.set_setting(session, "queue_running", not flags["queue_stopped"], commit=False)
        st.set_setting(session, "queue_cap", checked["queue_cap"], commit=False)
        st.set_setting(session, "insert_next", flags["insert_next"], commit=False)
        st.set_setting(session, "poll_minutes", checked["poll_minutes"], commit=False)
    if checked["poll_minutes"] != old_poll_minutes:
        # The loop waits for the next poll with the old interval; a regular tick now reads
        # the new one (a shorter interval would otherwise wait for the old one to end).
        request.app.state.ticker.request_poll()
    return _saved(request, tab=SECTION_TAB["automation"])
