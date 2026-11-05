"""CSRF protection: a token in the session cookie, echoed in every write."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

SESSION_KEY = "csrf"


def csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    """Dependency for every writing route: form field or `X-CSRF-Token` header."""
    expected = request.session.get(SESSION_KEY)
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied:
        form = await request.form()
        supplied = form.get("csrf_token")
    if (
        not expected
        or not isinstance(supplied, str)
        or not secrets.compare_digest(expected, supplied)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
