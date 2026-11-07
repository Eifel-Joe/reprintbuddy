"""Shared web helpers: database session, rendering and the redirect gate."""

from __future__ import annotations

import ipaddress
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from jinja2 import pass_context
from sqlalchemy.orm import Session

from reprintbuddy import auth
from reprintbuddy.bambuddy.client import BambuddyClient
from reprintbuddy.bambuddy.connection import ConnectionCheck
from reprintbuddy import settings as st
from reprintbuddy.models import User
from reprintbuddy.web import regions
from reprintbuddy.web.csrf import csrf_token
from reprintbuddy.web.i18n import translate, translate_count

UID_KEY = "uid"
SID_KEY = "sid"
FLASH_KEY = "flash"
FLASH_WARNING_KEY = "flash_warning"  # the message above is a warning, not good news
RIGHTS_LATER_KEY = "rights_later"  # in the session: the missing rights "Later" was pressed for
RIGHTS_SUCCESS_KEY = "rights_success"  # in the session: "Check again" in the dialog found every right

# Reachable by everyone, whatever the state of the installation.
_OPEN_EXACT = {"/healthz", "/favicon.ico"}
_OPEN_PREFIX = ("/static/",)


@pass_context
def t(context, key: str, **params: object) -> str:
    # {min_length} may be used in any text; it follows the one password-length constant.
    return translate(context.get("lang", "en"), key, min_length=auth.MIN_PASSWORD_LENGTH, **params)


@pass_context
def tc(context, key: str, n: int, **params: object) -> str:
    """`t` for a count: the text for one or for several (`<key>.one` / `<key>.other`)."""
    return translate_count(context.get("lang", "en"), key, n, **params)


def db_session(request: Request) -> Session:
    """The one database session of this request, created on first use.

    The gate, the route, the page rendering and the error pages all share it, so a
    request never needs a second connection while it holds the first. The gate
    closes it when the request is done.
    """
    db = getattr(request.state, "db", None)
    if db is None:
        db = request.app.state.sessionmaker()
        request.state.db = db
    elif not db.is_active:
        db.rollback()  # a failed flush leaves the session unusable until rolled back
    return db


def release_connection(db: Session) -> None:
    """Hand the database connection back before waiting (network, pause, queue).

    Only for moments when nothing is pending: it ends the transaction by rolling back.
    """
    db.rollback()


def close_request_session(request: Request) -> None:
    db = getattr(request.state, "db", None)
    if db is not None:
        request.state.db = None
        db.close()


def get_bambuddy(request: Request) -> BambuddyClient | None:
    """The client for Bambuddy, or None when no connection is available (B2 sets the factory;
    tests set a fake)."""
    factory = getattr(request.app.state, "bambuddy_factory", None)
    return factory() if factory is not None else None


async def get_db(request: Request) -> Session:
    return db_session(request)


def current_language(request: Request) -> str:
    return str(st.get_setting(db_session(request), "language"))


def render(request: Request, name: str, status_code: int = 200, **context: object) -> Response:
    """The page `name`; or, when the page asks for regions of itself, only those regions.

    Region mode (Spec 10.5.3): a GET with `X-Live-Regions`. No message is used up, whatever the
    answer: with two tabs on one cookie, the message "booked" belongs to the tab that booked and
    follows its redirect, not to the other tab that happens to fetch its regions first, nor to
    an error page that tab gets because the thing was deleted meanwhile. Only a page that
    answers 200 is cut: the whole page is rendered with the same template and the named
    pieces are returned.
    """
    session = db_session(request)
    asked = regions.requested(request)
    wanted = asked if status_code == 200 else []
    context.setdefault("lang", str(st.get_setting(session, "language")))
    # Target of the "Open Bambuddy" link; the key is never part of it.
    context["open_bambuddy_url"] = st.get_setting(session, "bambuddy_url")
    context.setdefault("active_nav", None)
    context["min_password_length"] = auth.MIN_PASSWORD_LENGTH
    if asked:
        context.setdefault("notice", None)
        context.setdefault("notice_warning", False)
    else:
        context.setdefault("notice", request.session.pop(FLASH_KEY, None))
        context.setdefault("notice_warning", bool(request.session.pop(FLASH_WARNING_KEY, False)))
    context["csrf_token"] = csrf_token(request)
    # The state the gate took before the data was read: a page is never newer than its state.
    live_state = getattr(request.state, "live_state", None)
    context["live_state"] = live_state if live_state is not None else request.app.state.live.state
    # Where the page fetches its regions: its own address. A page that answers a POST has none.
    context["live_src"] = _own_address(request) if request.method == "GET" else None
    context["live_region_mode"] = bool(asked)  # base.html then leaves the queued messages
    _add_rights_notice(request, context)
    response = request.app.state.templates.TemplateResponse(
        request, name, context, status_code=status_code
    )
    if not wanted:
        return response
    pieces = regions.extract(response.body.decode("utf-8"), wanted)
    if not pieces:  # not a page with these regions (the sign-in page, say): the whole page
        return response
    return HTMLResponse(
        "".join(pieces.values()),
        headers={
            regions.HEADER: " ".join(pieces),
            "Cache-Control": "no-store",
            "Vary": regions.HEADER,
        },
    )


def _own_address(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _page_to_return_to(request: Request) -> str:
    """Where the buttons of the rights notice lead back to.

    The page itself when it was shown for a GET. A page that is the answer to a POST
    (a failed save, an error page) has an address that only answers POST, so then the
    page the form came from: the Referer, if it is from this very server; else the start page.
    """
    if request.method == "GET":
        return _own_address(request)
    referer = urlsplit(request.headers.get("referer", ""))
    if referer.netloc and referer.netloc == request.headers.get("host"):
        return safe_next(referer.path + (f"?{referer.query}" if referer.query else ""))
    return "/"


def _add_rights_notice(request: Request, context: dict[str, object]) -> None:
    """What base.html needs to show the missing-rights dialog, after "Later" the warning line, or
    after a successful "Check again" in the dialog the same dialog in green (Spec 11.3, p31)."""
    notice = request.app.state.rights.notice
    context["rights_notice"] = notice
    # The green dialog stays until "Continue", but only while this process has really found every
    # right: the flag lives in the cookie and outlasts a restart, after which nothing may be claimed
    # until Bambuddy has answered (Spec 11.3). A right that goes missing again outranks it.
    context["rights_success"] = request.app.state.rights.status == "ok" and bool(
        request.session.get(RIGHTS_SUCCESS_KEY)
    )
    if notice is None:
        context["rights_later"] = False
        if context["rights_success"]:
            _add_permission_tables(context)
            context["rights_next"] = _page_to_return_to(request)
        return
    # "Later" holds for the rights it was pressed for. One more right missing after a later
    # update is news: the dialog comes back.
    context["rights_later"] = request.session.get(RIGHTS_LATER_KEY) == ",".join(notice.missing)
    labels = _add_permission_tables(context)["editor_labels"]
    context["rights_names"] = ", ".join(str(labels[code]) for code in notice.missing)
    context["rights_next"] = _page_to_return_to(request)


def _add_permission_tables(context: dict[str, object]) -> dict[str, object]:
    from reprintbuddy.web.bambuddy_check import permission_context  # imports deps: not at the top

    tables = permission_context()
    for name, value in tables.items():
        context.setdefault(name, value)
    return tables


def take_verdict(request: Request, check: ConnectionCheck) -> None:
    """A check was made with the stored key (settings, setup, "Check again"): the monitor takes
    the verdict. A key that has every right also ends a "Later"."""
    request.app.state.rights.record(check)
    # A new verdict ends a green dialog left open earlier; "Check again" in the dialog sets it anew.
    request.session.pop(RIGHTS_SUCCESS_KEY, None)
    if check.ok:
        request.session.pop(RIGHTS_LATER_KEY, None)


def flash(request: Request, key: str, *, warning: bool = False) -> None:
    """Remember a message (a translation key) for the next page shown; `warning` for anything
    that is not good news, which is shown in the look of an error."""
    request.session[FLASH_KEY] = key
    if warning:
        request.session[FLASH_WARNING_KEY] = True
    else:
        request.session.pop(FLASH_WARNING_KEY, None)


def safe_next(value: str | None) -> str:
    """Only local paths are valid redirect targets after login."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def local_next(form) -> str:
    """The local path in the form's `next` field: the list an action started from. "" when there is
    none or it is foreign (`safe_next` turns anything non-local into "/", which counts as none)."""
    target = safe_next(str(form.get("next", "")))
    return "" if target == "/" else target


def logged_in_user(request: Request, session: Session) -> User | None:
    """The user of a live session; None when the cookie is missing, forged or the session ended."""
    uid = request.session.get(UID_KEY)
    if uid is None:
        return None
    user = session.get(User, uid)
    if user is None or not auth.session_valid(session, request.session.get(SID_KEY)):
        return None
    return user


def start_session(request: Request, user: User, session: Session, *, commit: bool = True) -> None:
    """Log in: drop whatever the old cookie held (incl. its CSRF token) and start a
    new server-side session."""
    request.session.clear()
    request.session[UID_KEY] = user.id
    request.session[SID_KEY] = auth.create_session(session, commit=commit)


SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

# Name endings that are reserved or unused on the public internet. Names under a public
# top-level domain (for example .box, used by some routers) are not on this list: anybody
# can register such a name and point it at a private address.
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa")


async def security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def host_allowed_for_setup(host_header: str | None) -> bool:
    """Is this `Host` header plausible for someone on the home network or VPN?

    While no account exists, anybody who reaches the page could create it. A web
    page on the internet can make a visitor's browser talk to the home network or VPN by
    pointing its own name at a private address (DNS rebinding); the request then
    carries that name in `Host`. During setup only IP addresses, `localhost`,
    names without a dot and typical home-network endings are accepted.
    """
    host = (host_header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):  # IPv6 literal, possibly with a port
        name = host[1 : host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        name = host.split(":", 1)[0]
    else:
        name = host
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or "." not in name or name.endswith(_LOCAL_SUFFIXES)


def error_response(request: Request, status_code: int, kind: str) -> Response:
    """A readable error page in the interface language, inside the layout when signed in."""
    session = db_session(request)
    in_app = logged_in_user(request, session) is not None and st.bambuddy_configured(session)
    return render(
        request,
        "error.html",
        status_code=status_code,
        layout="base.html" if in_app else "bare.html",
        title_key=f"error.{kind}.title",
        message_key=f"error.{kind}.message",
    )


async def overflow_handler(request: Request, exc: OverflowError) -> Response:
    """A number in the address too large for SQLite names nothing that exists: 404."""
    if "SQLite INTEGER" in str(exc):
        return error_response(request, 404, "not_found")
    return error_response(request, 500, "generic")


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    kind = {403: "forbidden", 404: "not_found"}.get(exc.status_code, "generic")
    response = error_response(request, exc.status_code, kind)
    for name, value in (exc.headers or {}).items():  # for example Allow on a 405
        response.headers[name] = value
    return response


def stored_credentials(app) -> tuple[str, str] | None:
    """The stored Bambuddy address and key, or None while the setup is not finished.

    Reads in a session of its own and closes it before anybody asks Bambuddy.
    """
    with app.state.sessionmaker() as session:
        if not st.bambuddy_configured(session):
            return None
        return str(st.get_setting(session, "bambuddy_url")), str(st.get_setting(session, "bambuddy_api_key"))


def _decide(request: Request) -> Response | None:
    """The gate's decision: a response to send instead of the page, or None to let it through."""
    path = request.url.path
    if path in _OPEN_EXACT or path.startswith(_OPEN_PREFIX):
        return None

    session = db_session(request)
    try:
        if not auth.has_user(session):
            if not host_allowed_for_setup(request.headers.get("host")):
                return error_response(request, 400, "host")
            if path == "/setup":
                return None
            return RedirectResponse("/setup", status_code=303)

        # With an account, /setup answers 404 itself; /login is always open.
        if path in ("/setup", "/login"):
            return None

        if logged_in_user(request, session) is None:
            if request.method == "GET":
                target = path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
            return error_response(request, 403, "forbidden")

        in_bambuddy_setup = path == "/setup/bambuddy" or path.startswith("/setup/bambuddy/")
        if not st.bambuddy_configured(session) and not in_bambuddy_setup and path != "/logout":
            return RedirectResponse("/setup/bambuddy", status_code=303)
        # Signed in and set up: a page of the interface. The first one after a start also
        # checks what the stored key may do (Spec 11.3, "Rights after an update").
        request.state.rights_due = st.bambuddy_configured(session) and not in_bambuddy_setup
        return None
    finally:
        release_connection(session)  # the route starts without a connection held by the gate


async def gate(request: Request, call_next) -> Response:
    """Redirect order (Spec 11.3): setup -> login -> Bambuddy step -> page.

    The decision is taken first and the page runs afterwards; the request's database
    session is closed when the request is over.
    """
    # Taken before anything is read: a change saved while the page is drawn makes the page's
    # state older than the live channel's, so the page fetches again instead of missing it.
    request.state.live_state = request.app.state.live.state
    try:
        early = _decide(request)
        if early is not None:
            return early
        if getattr(request.state, "rights_due", False) and request.method == "GET":
            await request.app.state.rights.ensure(
                lambda: stored_credentials(request.app), request.app.state.http_client_factory
            )
        return await call_next(request)
    finally:
        close_request_session(request)
