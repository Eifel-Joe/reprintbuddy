"""Shared web helpers: database session, rendering and the redirect gate."""

from __future__ import annotations

from typing import AsyncIterator
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from jinja2 import pass_context
from sqlalchemy.orm import Session

from lagerbuddy import auth
from lagerbuddy import settings as st
from lagerbuddy.models import User
from lagerbuddy.web.csrf import csrf_token
from lagerbuddy.web.i18n import translate

UID_KEY = "uid"

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
    context.setdefault("lang", current_language(request))
    context["csrf_token"] = csrf_token(request)
    return request.app.state.templates.TemplateResponse(
        request, name, context, status_code=status_code
    )


def safe_next(value: str | None) -> str:
    """Only local paths are valid redirect targets after login."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def logged_in_user(request: Request, session: Session) -> User | None:
    uid = request.session.get(UID_KEY)
    return session.get(User, uid) if uid is not None else None


def start_session(request: Request, user: User) -> None:
    """Log in: drop whatever the old session held (incl. its CSRF token)."""
    request.session.clear()
    request.session[UID_KEY] = user.id


async def gate(request: Request, call_next) -> Response:
    """Redirect order (Spec 11.3): setup -> login -> Bambuddy step -> page."""
    path = request.url.path
    if path in _OPEN_EXACT or path.startswith(_OPEN_PREFIX):
        return await call_next(request)

    with request.app.state.sessionmaker() as session:
        if not auth.has_user(session):
            if path == "/setup":
                return await call_next(request)
            return RedirectResponse("/setup", status_code=303)

        # With an account, /setup answers 404 itself; /login is always open.
        if path in ("/setup", "/login"):
            return await call_next(request)

        if logged_in_user(request, session) is None:
            if request.method == "GET":
                return RedirectResponse(f"/login?next={quote(path, safe='')}", status_code=303)
            return Response("Forbidden", status_code=403, media_type="text/plain")

        if not st.bambuddy_configured(session) and path not in ("/setup/bambuddy", "/logout"):
            return RedirectResponse("/setup/bambuddy", status_code=303)

    return await call_next(request)
