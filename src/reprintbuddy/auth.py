"""The single admin account. Passwords are stored only as argon2 hashes."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from reprintbuddy.models import LoginSession, User

MIN_PASSWORD_LENGTH = 8

_hasher = PasswordHasher()
# Verified against when the username is unknown, so a miss costs the same time.
_DUMMY_HASH = _hasher.hash("reprintbuddy-timing-dummy")


def hash_password(pw: str) -> str:
    return _hasher.hash(pw)


def verify_password(hash_: str, pw: str) -> bool:
    try:
        return _hasher.verify(hash_, pw)
    except (VerificationError, InvalidHashError):
        return False


def _check_length(pw: str) -> None:
    if len(pw) < MIN_PASSWORD_LENGTH:
        raise ValueError("password too short")


# --- sessions ------------------------------------------------------------------------------
# The cookie holds a random session id; the table holds its SHA-256 and the times.
# Logging out deletes the own row, a password change deletes all rows.

SESSION_MAX_IDLE = timedelta(days=14)  # as long as the cookie lives
TOUCH_INTERVAL = timedelta(minutes=1)  # last_seen_at is written at most this often


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def session_id_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def prune_sessions(session: Session) -> None:
    session.execute(delete(LoginSession).where(LoginSession.last_seen_at < _utcnow() - SESSION_MAX_IDLE))


def create_session(session: Session, *, commit: bool = True) -> str:
    """Start a session and return the id for the cookie. Old idle sessions are removed."""
    prune_sessions(session)
    sid = secrets.token_urlsafe(32)
    now = _utcnow()
    session.add(LoginSession(id=session_id_hash(sid), created_at=now, last_seen_at=now))
    if commit:
        session.commit()
    else:
        session.flush()
    return sid


def session_valid(session: Session, sid: object) -> bool:
    """Is this a live session? Refreshes `last_seen_at` (at most once a minute)."""
    if not isinstance(sid, str):
        return False
    row = session.get(LoginSession, session_id_hash(sid))
    if row is None:
        return False
    now = _utcnow()
    if row.last_seen_at < now - SESSION_MAX_IDLE:
        session.delete(row)
        session.commit()
        return False
    if now - row.last_seen_at > TOUCH_INTERVAL:
        row.last_seen_at = now
        session.commit()
    return True


def end_session(session: Session, sid: object) -> None:
    """Log out one browser: its session id stops working, copies of the cookie included."""
    if isinstance(sid, str):
        session.execute(delete(LoginSession).where(LoginSession.id == session_id_hash(sid)))
        session.commit()


def end_all_sessions(session: Session, *, commit: bool = True) -> None:
    session.execute(delete(LoginSession))
    if commit:
        session.commit()


def clean_username(username: str) -> str:
    """Surrounding whitespace is dropped (as on the login form); an empty name is refused."""
    cleaned = username.strip()
    if not cleaned:
        raise ValueError("the user name must not be empty")
    return cleaned


def has_user(session: Session) -> bool:
    return session.scalars(select(User.id).limit(1)).first() is not None


def create_admin(session: Session, username: str, pw: str, *, commit: bool = True) -> User:
    """First-run setup: create the account; refuse if one already exists.

    With `commit=False` the caller commits (setup saves the account and the language
    in one transaction); the account is flushed, so it already has its id.
    """
    username = clean_username(username)
    _check_length(pw)
    if has_user(session):
        raise ValueError("an account already exists")
    user = User(username=username, password_hash=hash_password(pw))
    session.add(user)
    if commit:
        session.commit()
    else:
        session.flush()
    return user


def set_password(session: Session, username: str, pw: str) -> None:
    """Create the account, or change the existing one (there is only one).

    Used by the `set-password` command and the password-change form. An
    existing account takes over the given username, so a forgotten username
    is no obstacle to getting back in.
    """
    username = clean_username(username)
    _check_length(pw)
    user = session.scalars(select(User).limit(1)).first()
    if user is None:
        user = User(username=username, password_hash=hash_password(pw))
        session.add(user)
    else:
        user.username = username
        user.password_hash = hash_password(pw)
    end_all_sessions(session, commit=False)  # every session ends with the old password
    session.commit()


def authenticate(session: Session, username: str, pw: str) -> User | None:
    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None:
        verify_password(_DUMMY_HASH, pw)
        return None
    return user if verify_password(user.password_hash, pw) else None
