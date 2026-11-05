"""Shared web helpers: database session, rendering and the redirect gate."""

from __future__ import annotations

import hmac
import ipaddress
from typing import AsyncIterator
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from jinja2 import pass_context
from sqlalchemy.orm import Session

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.models import User
from reprintbuddy.web.csrf import csrf_token
from reprintbuddy.web.i18n import translate

UID_KEY = "uid"
TOKEN_KEY = "tok"
FLASH_KEY = "flash"

# Reachable by everyone, whatever the state of the installation.
_OPEN_EXACT = {"/healthz"}
_OPEN_PREFIX = ("/static/",)


@pass_context
def t(context, key: str, **params: object) -> str:
    return translate(context.get("lang", "en"), key, **params)


async def get_db(request: Request) -> AsyncIterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


def current_language(request: Request) -> str:
    with request.app.state.sessionmaker() as session:
        return str(st.get_setting(session, "language"))


def render(request: Request, name: str, status_code: int = 200, **context: object) -> Response:
    with request.app.state.sessionmaker() as session:
        context.setdefault("lang", str(st.get_setting(session, "language")))
        # Target of the "Open Bambuddy" link; the key is never part of it.
        context["open_bambuddy_url"] = st.get_setting(session, "bambuddy_url")
    context.setdefault("active_nav", None)
    context.setdefault("notice", request.session.pop(FLASH_KEY, None))
    context["csrf_token"] = csrf_token(request)
    return request.app.state.templates.TemplateResponse(
        request, name, context, status_code=status_code
    )


def flash(request: Request, key: str) -> None:
    """Remember a message (a translation key) for the next page shown."""
    request.session[FLASH_KEY] = key


def safe_next(value: str | None) -> str:
    """Only local paths are valid redirect targets after login."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def _expected_token(request: Request, session: Session, user: User) -> str:
    return auth.session_token(
        request.app.state.session_secret, user.password_hash, st.session_generation(session)
    )


def logged_in_user(request: Request, session: Session) -> User | None:
    """The user of a valid session; None when the cookie is missing, forged or revoked."""
    uid = request.session.get(UID_KEY)
    supplied = request.session.get(TOKEN_KEY)
    if uid is None or not isinstance(supplied, str):
        return None
    user = session.get(User, uid)
    if user is None or not hmac.compare_digest(supplied, _expected_token(request, session, user)):
        return None
    return user


def start_session(request: Request, user: User, session: Session) -> None:
    """Log in: drop whatever the old session held (incl. its CSRF token) and bind
    the new one to the current password and session generation."""
    request.session.clear()
    request.session[UID_KEY] = user.id
    request.session[TOKEN_KEY] = _expected_token(request, session, user)


SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

# Name endings that only occur inside a home network.
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa", ".fritz.box")


async def security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def host_allowed_for_setup(host_header: str | None) -> bool:
    """Is this `Host` header plausible for someone on the home network?

    While no account exists, anybody who reaches the page could create it. A web
    page on the internet can make a visitor's browser talk to the home network by
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
    with request.app.state.sessionmaker() as session:
        in_app = logged_in_user(request, session) is not None and st.bambuddy_configured(session)
    return render(
        request,
        "error.html",
        status_code=status_code,
        layout="base.html" if in_app else "bare.html",
        title_key=f"error.{kind}.title",
        message_key=f"error.{kind}.message",
    )


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    kind = {403: "forbidden", 404: "not_found"}.get(exc.status_code, "generic")
    response = error_response(request, exc.status_code, kind)
    for name, value in (exc.headers or {}).items():  # for example Allow on a 405
        response.headers[name] = value
    return response


async def gate(request: Request, call_next) -> Response:
    """Redirect order (Spec 11.3): setup -> login -> Bambuddy step -> page."""
    path = request.url.path
    if path in _OPEN_EXACT or path.startswith(_OPEN_PREFIX):
        return await call_next(request)

    with request.app.state.sessionmaker() as session:
        if not auth.has_user(session):
            if not host_allowed_for_setup(request.headers.get("host")):
                return error_response(request, 400, "host")
            if path == "/setup":
                return await call_next(request)
            return RedirectResponse("/setup", status_code=303)

        # With an account, /setup answers 404 itself; /login is always open.
        if path in ("/setup", "/login"):
            return await call_next(request)

        if logged_in_user(request, session) is None:
            if request.method == "GET":
                target = path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
            return error_response(request, 403, "forbidden")

        if not st.bambuddy_configured(session) and path not in ("/setup/bambuddy", "/logout"):
            return RedirectResponse("/setup/bambuddy", status_code=303)

    return await call_next(request)
