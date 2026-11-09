"""The settings of the push messages through Home Assistant and the test message (Spec 10.6).

The token follows the rule of the Bambuddy key (Spec 11.5a, 10.6 "Sicherheit"): it is shown only
while it is typed; an empty field keeps the stored token, but only for the same address (scheme,
host, port), so a stored token never travels to another address. The test message sends what is
typed without saving it and shows a fixed text for the outcome, never Home Assistant's own answer.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.notify import events
from reprintbuddy.notify import homeassistant as ha
from reprintbuddy.web import qr as qr_view
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, release_connection
from reprintbuddy.web.flash import flash
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.routes.ops import ops_ready
from reprintbuddy.web.routes.settings import SECTION_TAB, _page, _saved

router = APIRouter()

SECTION = "notify"


def _typed(form) -> tuple[dict[str, object], str]:
    """What the form holds, as the page shows it again (no token), and the typed token."""
    typed: dict[str, object] = {
        "ha_enabled": form.get("ha_enabled") == "on",
        "ha_url": str(form.get("ha_url", "")).strip(),
        "ha_service": str(form.get("ha_service", "")).strip(),
        "ha_names": form.get("ha_names") == "on",
        "ha_events": [str(e) for e in form.getlist("ha_event")],
    }
    return typed, str(form.get("ha_token", "")).strip()


def _checked(
    db: Session, typed: dict[str, object], entered_token: str, *, required: bool
) -> tuple[str | None, str | None, str | None, list[str]] | str:
    """(address, token, service, events) to store or send, or the key of the error."""
    try:
        url = st.validate("ha_url", typed["ha_url"]) if typed["ha_url"] else None
    except ValueError:
        return "notify.invalid_url"
    try:
        service = st.validate("ha_service", typed["ha_service"]) if typed["ha_service"] else None
    except ValueError:
        return "notify.invalid_service"
    try:
        chosen = st.validate("ha_events", typed["ha_events"])
    except ValueError:
        return "settings.invalid_value"
    stored_url = str(st.get_setting(db, "ha_url") or "")
    stored_token = st.get_setting(db, "ha_token")
    if entered_token:
        try:
            token = st.validate("ha_token", entered_token)
        except ValueError:
            return "notify.invalid_token"
    elif stored_token and url and st.same_address(str(url), stored_url):
        token = stored_token
    else:
        if required and stored_token and url:
            return "notify.token_required_new_address"
        token = None  # a stored token never goes with another address
    if required and not (url and token and service):
        return "notify.fields_required"
    return url, token, service, chosen  # type: ignore[return-value]


@router.post("/settings/notify", dependencies=[Depends(verify_csrf)])
async def save_notify(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    typed, entered_token = _typed(form)
    enabled = bool(typed["ha_enabled"])
    result = _checked(db, typed, entered_token, required=enabled)
    if isinstance(result, str):
        return _page(request, db, result, SECTION, busy=await ops_ready(request), **typed)
    url, token, service, chosen = result
    was_on = bool(st.get_setting(db, "ha_enabled"))
    with write_transaction(request) as tx:  # all or nothing
        st.set_setting(tx, "ha_enabled", enabled, commit=False)
        st.set_setting(tx, "ha_url", url, commit=False)
        st.set_setting(tx, "ha_token", token, commit=False)
        st.set_setting(tx, "ha_service", service, commit=False)
        st.set_setting(tx, "ha_names", bool(typed["ha_names"]), commit=False)
        st.set_setting(tx, "ha_events", chosen, commit=False)
        if enabled and (not was_on or st.get_setting(tx, "ha_since") is None):
            # From now on (plan D1): prints finished before are never reported.
            st.set_setting(tx, "ha_since", datetime.now(UTC).isoformat(), commit=False)
        if not enabled:
            # Switched off, nothing is followed; switched on again, everything starts anew.
            st.set_setting(tx, "ha_since", None, commit=False)
            events.forget_all(tx)
    return _saved(request, tab=SECTION_TAB[SECTION])


@router.post("/settings/notify/test", dependencies=[Depends(verify_csrf)])
async def send_test_message(request: Request, db: Session = Depends(get_db)):
    """One fixed message with what is typed, without saving it; the page is shown again with what was
    typed and the outcome (a redirect would lose the unsaved input)."""
    form = await request.form()
    typed, entered_token = _typed(form)
    result = _checked(db, typed, entered_token, required=True)
    busy = await ops_ready(request)
    if isinstance(result, str):
        return _page(request, db, result, SECTION, busy=busy, **typed)
    url, token, service, _ = result
    try:  # the one-time token is used up and committed first: the call below holds no lock
        with form_transaction(request, str(form.get("form_token", ""))):
            pass
    except FormTokenUsed:
        flash(request, "form.duplicate")
        return _page(request, db, None, SECTION, busy=busy, **typed)
    except FormTokenInvalid:
        flash(request, "form.expired", warning=True)
        return _page(request, db, None, SECTION, busy=busy, **typed)
    lang = current_language(request)
    message = ha.Message(
        translate(lang, "notify.test.title"),
        translate(lang, "notify.test.text"),
        f"{qr_view.base_address(request, db).rstrip('/')}/",
    )
    release_connection(db)  # nothing may hold the database while Home Assistant takes its time
    outcome = await ha.send(request.app.state.http_client_factory, str(url), str(token), str(service), message)
    # A typed token is never written back into the page (Spec 11.5a): saving needs it once more.
    return _page(
        request, db, None, SECTION, busy=busy, notify_test=outcome, notify_retype=bool(entered_token), **typed
    )
