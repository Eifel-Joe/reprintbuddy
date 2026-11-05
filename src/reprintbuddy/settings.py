"""All settings live in the database (table `settings`, JSON values).

There is no configuration file. Later building blocks add their keys here.
Sensitive values are only ever shown as `MASK`; see `display_value`.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Callable
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from reprintbuddy.models import Setting

MASK = "********"

DEFAULTS: dict[str, object] = {
    "language": "en",
    "poll_minutes": 15,
    "queue_cap": 10,
    "insert_next": False,
    "automation": False,
    "queue_running": True,
    "bambuddy_url": None,
    "bambuddy_username": None,
    "bambuddy_api_key": None,
    "bambuddy_review_confirmed": False,
    "host": "0.0.0.0",
    "port": 8080,
    "tls_cert": None,
    "tls_key": None,
}

SENSITIVE: frozenset[str] = frozenset({"bambuddy_api_key", "tls_key"})

# Never part of an export, a log line or any page: the sensitive settings plus the
# values Reprintbuddy keeps for itself.
NEVER_EXPORT: frozenset[str] = SENSITIVE | {"session_secret", "session_generation"}

_SECRET_KEY = "session_secret"
_GENERATION_KEY = "session_generation"
LANGUAGES = ("en", "de")


# --- validators: return the value to store, or raise ValueError -------------
# Error messages never contain the offending value (it may be a secret).


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected true or false")
    return value


def _int_range(low: int, high: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("expected a whole number")
        if not low <= value <= high:
            raise ValueError(f"expected a number from {low} to {high}")
        return value

    return check


def _language(value: Any) -> str:
    if value not in LANGUAGES:
        raise ValueError("unknown language")
    return value


def _nonempty_str(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must not be empty")
    return value


def _optional_nonempty(value: Any) -> str | None:
    if value is None:
        return None
    return _nonempty_str(value)


def _api_key(value: Any) -> str | None:
    """Surrounding whitespace is dropped; the rest must be visible ASCII.

    The key travels in an HTTP header, where anything else either fails with an
    unrelated error or is silently altered (a pasted line break, a zero-width space).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected text")
    key = value.strip()
    if not key:
        raise ValueError("must not be empty")
    if not all("!" <= char <= "~" for char in key):
        raise ValueError("contains characters that are not allowed")
    return key


def _optional_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected an address")
    text = value.strip().rstrip("/")
    for scheme in ("http://", "https://"):
        if text.startswith(scheme) and len(text) > len(scheme):
            try:
                parts = urlsplit(text)
                port = parts.port  # raises ValueError for a non-numeric or out-of-range port
            except ValueError:
                raise ValueError("address has an invalid host or port") from None
            if not parts.hostname or port == 0:
                raise ValueError("address has an invalid host or port")
            return text
    raise ValueError("address must start with http:// or https://")


def _optional_pem(value: Any) -> str | None:
    if value is None:
        return None
    text = _nonempty_str(value)
    if not text.lstrip().startswith("-----BEGIN"):
        raise ValueError("expected a PEM text")
    return text


_VALIDATORS: dict[str, Callable[[Any], object]] = {
    "language": _language,
    "poll_minutes": _int_range(1, 1440),
    "queue_cap": _int_range(1, 999),
    "insert_next": _bool,
    "automation": _bool,
    "queue_running": _bool,
    "bambuddy_url": _optional_url,
    "bambuddy_username": _optional_nonempty,
    "bambuddy_api_key": _api_key,
    "bambuddy_review_confirmed": _bool,
    "host": _nonempty_str,
    "port": _int_range(1, 65535),
    "tls_cert": _optional_pem,
    "tls_key": _optional_pem,
}


def get_setting(session: Session, key: str) -> object:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = session.get(Setting, key)
    if row is None:
        return DEFAULTS[key]
    return json.loads(row.value)


def validate(key: str, value: object) -> object:
    """Check `value` for `key` and return what would be stored (normalised)."""
    if key not in _VALIDATORS:
        raise KeyError(key)
    return _VALIDATORS[key](value)


def set_setting(session: Session, key: str, value: object, *, commit: bool = True) -> None:
    """Validate and store a setting.

    By default the change is committed at once. Code that changes several things
    in one transaction (stock bookings, for example) passes `commit=False` and
    commits or rolls back itself; the new value is already visible to this session.
    """
    checked = validate(key, value)
    session.merge(Setting(key=key, value=json.dumps(checked)))
    if commit:
        session.commit()


def ensure_session_secret(session: Session) -> str:
    """Return the cookie-signing secret; create it on the very first call."""
    row = session.get(Setting, _SECRET_KEY)
    if row is not None:
        return json.loads(row.value)
    secret = secrets.token_urlsafe(48)
    session.add(Setting(key=_SECRET_KEY, value=json.dumps(secret)))
    session.commit()
    return secret


def session_generation(session: Session) -> int:
    """Counter that ends all sessions when it is raised (log out)."""
    row = session.get(Setting, _GENERATION_KEY)
    return int(json.loads(row.value)) if row is not None else 0


def bump_session_generation(session: Session) -> None:
    session.merge(Setting(key=_GENERATION_KEY, value=json.dumps(session_generation(session) + 1)))
    session.commit()


def display_value(key: str, value: object) -> str:
    """What a page may show: sensitive values only as `MASK`."""
    if key in SENSITIVE:
        return MASK if value else ""
    if value is None:
        return ""
    return str(value)


def tls_enabled(session: Session) -> bool:
    """HTTPS is on only when both the certificate and the private key are stored."""
    return bool(get_setting(session, "tls_cert") and get_setting(session, "tls_key"))


def bambuddy_configured(session: Session) -> bool:
    return bool(
        get_setting(session, "bambuddy_url")
        and get_setting(session, "bambuddy_username")
        and get_setting(session, "bambuddy_api_key")
        and get_setting(session, "bambuddy_review_confirmed")
    )


def same_address(a: str | None, b: str | None) -> bool:
    """Do two stored/entered addresses point at the same scheme, host and port?

    Used to decide whether a stored API key may be sent to an address: the key
    belongs to one Bambuddy address and never travels to another.
    """
    if not a or not b:
        return False

    def origin(url: str) -> tuple[str, str, int | None]:
        parts = urlsplit(url.strip())
        default = {"http": 80, "https": 443}.get(parts.scheme)
        return parts.scheme, (parts.hostname or "").lower(), parts.port or default

    try:
        return origin(a) == origin(b)
    except ValueError:  # an invalid port or address is never "the same"
        return False
