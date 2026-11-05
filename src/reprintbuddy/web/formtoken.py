"""One-time form tokens against double submission (Review Focus 1).

A token is signed (`itsdangerous`), so a GET page needs no database write to issue
one. It is used up by an INSERT into `form_tokens` inside the very transaction that
books; a second submission of the same form hits the same primary key and is
reported as "already submitted". When the booking is rejected the transaction is
rolled back and the token is free again for the corrected form.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from reprintbuddy.db import begin_immediate
from reprintbuddy.models import FormToken, utcnow
from reprintbuddy.web.deps import db_session

SALT = "form-token"
CLEANUP_AFTER = timedelta(days=2)


class FormTokenInvalid(Exception):
    """Missing, forged or expired."""


class FormTokenUsed(Exception):
    """Already used by an earlier submission."""


def issue_form_token(secret: str) -> str:
    return URLSafeTimedSerializer(secret, salt=SALT).dumps(secrets.token_urlsafe(16))


def consume_form_token(session: Session, secret: str, token: str, max_age_s: int = 86_400) -> None:
    """Use the token up in this transaction; call it as the first statement of a booking."""
    try:
        payload = URLSafeTimedSerializer(secret, salt=SALT).loads(token, max_age=max_age_s)
    except BadSignature as exc:  # SignatureExpired is a BadSignature
        raise FormTokenInvalid from exc
    if not isinstance(payload, str) or not payload:
        raise FormTokenInvalid
    now = utcnow()
    if session.get(FormToken, payload) is not None:
        raise FormTokenUsed
    session.execute(delete(FormToken).where(FormToken.used_at < now - CLEANUP_AFTER))
    session.add(FormToken(token=payload, used_at=now))
    try:
        session.flush()
    except IntegrityError as exc:  # a concurrent submission got there first
        raise FormTokenUsed from exc


@contextmanager
def form_transaction(request: Request, token: str | None) -> Iterator[Session]:
    """The write transaction of a form: starts immediately, uses the token up first.

    It uses the request's own database session (B1: one per request). Anything the
    session held before is released. If the body raises, nothing is written and the
    token stays usable.
    """
    if not token:
        raise FormTokenInvalid
    session = db_session(request)
    session.rollback()  # no read snapshot may survive into the write transaction
    with session.begin():
        begin_immediate(session)
        consume_form_token(session, request.app.state.session_secret, token)
        yield session
