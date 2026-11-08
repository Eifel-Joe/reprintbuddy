"""All settings live in the database (table `settings`, JSON values).

There is no configuration file. Later building blocks add their keys here.
Sensitive values are only ever shown as `MASK`; see `display_value`.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
from typing import Any, Callable
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.connection import ConnectionCheck
from reprintbuddy.labels import fields as label_fields
from reprintbuddy.labels.addresses import is_local_or_wildcard
from reprintbuddy.labels.rolls import DEFAULT_ROLL, ROLLS
from reprintbuddy.models import Setting

MASK = "********"

DEFAULTS: dict[str, object] = {
    "language": "en",
    "poll_minutes": 15,
    "queue_cap": 10,
    "insert_next": False,
    "automation": False,
    "queue_running": True,
    "update_check": True,  # the automatic check for new versions (Spec 11.3a)
    "qr_base_url": None,  # the start of the address inside the QR codes (Spec 8.4); none = the server's own network address (found by Reprintbuddy)
    "label_printer_host": None,  # the Brother label printer in the network (Spec 8.5); none = not set up
    "label_roll": DEFAULT_ROLL,  # the roll in it (Spec 8.5)
    "label_fields": list(label_fields.DEFAULT_FIELDS),  # the details on the label besides the QR code
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
NEVER_EXPORT: frozenset[str] = SENSITIVE | {"session_secret"}

_SECRET_KEY = "session_secret"
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


_HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", re.IGNORECASE)


def _qr_base_url(value: Any) -> str | None:
    """The start of the address a QR code carries (Spec 8.4): scheme, host, port and, behind a
    reverse proxy, a path. No credentials (the code is printed and stuck to a shelf), no query or
    fragment (the article's path is appended), no blank, control or backslash character."""
    if value is None:
        return None
    text = _optional_url(value)  # scheme, host, port; a trailing slash is dropped
    assert text is not None
    if any(char in text for char in "?#\\") or any(char.isspace() or not char.isprintable() for char in text):
        raise ValueError("address must be a plain web address without query or fragment")
    if "@" in text:  # user:password@host (also a path with an @ is not worth the doubt)
        raise ValueError("address must not contain credentials")
    parts = urlsplit(text)
    if parts.netloc.endswith(":"):  # a port sign without a port
        raise ValueError("address has an invalid host or port")
    if not parts.netloc.startswith("["):  # an IPv6 address in brackets was parsed by `_optional_url`
        try:
            labels = (parts.hostname or "").encode("idna").decode("ascii").split(".")  # umlauts are fine
        except UnicodeError:
            raise ValueError("address has an invalid host or port") from None
        if not all(_HOST_LABEL.fullmatch(label) for label in labels):
            raise ValueError("address has an invalid host or port")
    if any(char in parts.path for char in "<>\"'") or any(
        segment in (".", "..") for segment in parts.path.split("/")
    ):
        raise ValueError("address has an invalid path")
    return text


_HOSTNAME_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)


def _label_printer_host(value: Any) -> str | None:
    """The address of the label printer (Spec 8.5): an IP address or a name, nothing else (no web
    address, port, path or user), and never this machine itself: the printer is another device.
    Empty means not set up. The printer's address comes from here only, never from a request."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected an address")
    text = value.strip()
    if not text:
        return None
    if "%" in text:
        raise ValueError("expected an IP address or a name")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        name = text.rstrip(".")
        if len(name) > 253 or not all(_HOSTNAME_LABEL.fullmatch(label) for label in name.split(".")):
            raise ValueError("expected an IP address or a name") from None
        if name.lower() == "localhost" or name.lower().endswith(".localhost"):
            raise ValueError("the printer is another device, not this computer")
        return name
    if is_local_or_wildcard(str(address)):
        raise ValueError("the printer is another device, not this computer")
    return str(address)


def _label_roll(value: Any) -> str:
    if not isinstance(value, str) or value not in ROLLS:
        raise ValueError("unknown roll")
    return value


def _label_fields(value: Any) -> list[str]:
    """The chosen details, once each, in their fixed order."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item in label_fields.FIELDS for item in value):
        raise ValueError("unknown detail")
    return list(label_fields.clean(value))


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
    "update_check": _bool,
    "qr_base_url": _qr_base_url,
    "label_printer_host": _label_printer_host,
    "label_roll": _label_roll,
    "label_fields": _label_fields,
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


def apply_check(session: Session, url: object, api_key: str, check: ConnectionCheck) -> None:
    """Store the Bambuddy connection after a successful check, in one transaction.

    The user name is the owner Bambuddy reported for the key (it is never typed in), and
    the review flag becomes true because the check proved the right to print without review.
    """
    if not check.ok or not check.owner:
        raise ValueError("the connection check did not succeed")
    try:
        set_setting(session, "bambuddy_url", url, commit=False)
        set_setting(session, "bambuddy_username", check.owner, commit=False)
        set_setting(session, "bambuddy_api_key", api_key, commit=False)
        set_setting(session, "bambuddy_review_confirmed", True, commit=False)
        session.commit()
    except Exception:
        session.rollback()
        raise


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
