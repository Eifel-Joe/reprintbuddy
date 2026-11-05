"""The single admin account. Passwords are stored only as argon2 hashes."""

from __future__ import annotations

import hashlib
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.models import User

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


def session_token(secret: str, password_hash: str, generation: int) -> str:
    """Short HMAC kept in the session cookie.

    It changes with the password hash (password change, `set-password`) and with
    the session generation (log out), so such events end every older session.
    """
    message = f"{generation}:{password_hash}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:32]


def has_user(session: Session) -> bool:
    return session.scalars(select(User.id).limit(1)).first() is not None


def create_admin(session: Session, username: str, pw: str, *, commit: bool = True) -> User:
    """First-run setup: create the account; refuse if one already exists.

    With `commit=False` the caller commits (setup saves the account and the language
    in one transaction); the account is flushed, so it already has its id.
    """
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
    _check_length(pw)
    user = session.scalars(select(User).limit(1)).first()
    if user is None:
        user = User(username=username, password_hash=hash_password(pw))
        session.add(user)
    else:
        user.username = username
        user.password_hash = hash_password(pw)
    session.commit()


def authenticate(session: Session, username: str, pw: str) -> User | None:
    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None:
        verify_password(_DUMMY_HASH, pw)
        return None
    return user if verify_password(user.password_hash, pw) else None
