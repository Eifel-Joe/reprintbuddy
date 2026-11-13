"""All settings live in the database (table `settings`, JSON values).

There is no configuration file. Later building blocks add their keys here.
Sensitive values are only ever shown as `MASK`; see `display_value`.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.connection import ConnectionCheck
from reprintbuddy.labels import fields as label_fields
from reprintbuddy.labels.addresses import is_local_or_wildcard
from reprintbuddy.labels.rolls import DEFAULT_ROLL, ROLLS
from reprintbuddy.models import Setting

MASK = "********"

# The events of the push messages, in the order of Spec 10.6 (one tick box each).
HA_EVENTS: tuple[str, ...] = (
    "print_done",
    "print_failed",
    "paused",
    "below_min",
    "reorder",
    "trouble",
    "rights",
)

DEFAULTS: dict[str, object] = {
    "language": "en",
    "poll_minutes": 15,
    "queue_cap": 10,
    "insert_next": False,
    "automation": False,
    "queue_running": True,
    "update_check": True,  # the automatic check for new versions (Spec 11.3a)
    "update_channel": "stable",  # v0.4.9: "stable" releases only, or "prerelease"s too (Spec 11.3a)
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
    # Push messages through Home Assistant (Spec 10.6): off and empty until entered.
    "ha_enabled": False,
    "ha_url": None,
    "ha_token": None,
    "ha_service": None,
    "ha_names": True,  # name articles, spool sorts and parts in the messages
    "ha_events": list(HA_EVENTS),
    "ha_since": None,  # when the messages were switched on (plan D1); set by Reprintbuddy, not shown
    # Home Assistant refused the stored token (401): no automatic try until the card is saved again
    # (Spec 10.6 "Token abgelehnt", p152); set by Reprintbuddy, not shown, kept over a restart.
    "ha_token_rejected": False,
    # What "new" means on the overview (Spec 10.2, 0.4.1): set when /orders or /reorder is loaded,
    # shared by every device; set by Reprintbuddy, not shown. None: the page was never loaded.
    "orders_seen_id": None,  # the highest order id when /orders was last loaded
    "reorder_seen": None,  # the suggestions /reorder showed then, as "filament:3", "part:7"
}

SENSITIVE: frozenset[str] = frozenset({"bambuddy_api_key", "tls_key", "ha_token"})

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


UPDATE_CHANNELS = ("stable", "prerelease")


def _update_channel(value: Any) -> str:
    if value not in UPDATE_CHANNELS:
        raise ValueError("unknown choice of versions")
    return value


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
    # No blank, line break or control character inside: `urlsplit` drops line breaks and tabs when it
    # reads, so the checks below would pass while the stored text still carries them (v0.3.8 p113).
    if any(char.isspace() or not char.isprintable() for char in text):
        raise ValueError("address must not contain blanks or line breaks")
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


def _service_url(value: Any) -> str | None:
    """The address of Bambuddy (Spec 11.3 c) or Home Assistant (Spec 10.6): http or https, host, port
    and, behind a proxy, a path in front. No credentials, query or fragment: Reprintbuddy appends the
    path of each call, which would land behind a `#` or `?` and go nowhere (p165; until v0.3.12 only
    Home Assistant's address was checked like this). The QR address has its own, stricter rule."""
    if value is None:
        return None
    text = _optional_url(value)
    assert text is not None
    if any(char in text for char in "?#\\") or "@" in text:
        raise ValueError("address must be a plain web address without credentials, query or fragment")
    if urlsplit(text).netloc.endswith(":"):
        raise ValueError("address has an invalid host or port")
    return text


# Only services of the `notify` domain, never `notify.send_message` (it knows no link): Spec 10.6.
# The pattern also keeps the path of the call fixed: no slash, dot or blank after the domain.
_HA_SERVICE = re.compile(r"notify\.[a-z0-9_]+")


def _ha_service(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected a service name")
    name = value.strip()
    if not _HA_SERVICE.fullmatch(name) or name == "notify.send_message":
        raise ValueError("expected a service of the notify domain, such as notify.mobile_app_phone")
    return name


def _ha_events(value: Any) -> list[str]:
    """The ticked events, once each, in the order of `HA_EVENTS`."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item in HA_EVENTS for item in value):
        raise ValueError("unknown event")
    return [event for event in HA_EVENTS if event in value]


def _ha_since(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected a point in time")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("expected a point in time") from None
    if moment.tzinfo is None:
        raise ValueError("expected a point in time with its zone")
    return value


def _seen_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected an order id")
    return value


def _seen_keys(value: Any) -> list[str] | None:
    """The suggestions shown, sorted and once each; "filament:<id>" or "part:<id>"."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("expected a list of suggestions")
    for item in value:
        kind, _, number = item.partition(":") if isinstance(item, str) else ("", "", "")
        if kind not in ("filament", "part") or not number.isdigit():
            raise ValueError("expected a list of suggestions")
    return sorted(set(value))


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
    "update_channel": _update_channel,
    "qr_base_url": _qr_base_url,
    "label_printer_host": _label_printer_host,
    "label_roll": _label_roll,
    "label_fields": _label_fields,
    "bambuddy_url": _service_url,
    "bambuddy_username": _optional_nonempty,
    "bambuddy_api_key": _api_key,
    "bambuddy_review_confirmed": _bool,
    "host": _nonempty_str,
    "port": _int_range(1, 65535),
    "tls_cert": _optional_pem,
    "tls_key": _optional_pem,
    "ha_enabled": _bool,
    "ha_url": _service_url,
    "ha_token": _api_key,  # a long-lived access token: visible ASCII, like an API key
    "ha_service": _ha_service,
    "ha_names": _bool,
    "ha_events": _ha_events,
    "ha_since": _ha_since,
    "ha_token_rejected": _bool,
    "orders_seen_id": _seen_id,
    "reorder_seen": _seen_keys,
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


# What an earlier version stored under a rule since made stricter. Such a value still works where it is
# read and is fixed the next time it is saved; it must not make a backup unrestorable (review of p165).
_EARLIER_RULES: dict[str, Callable[[Any], object]] = {"bambuddy_url": _optional_url}


def validate_stored(key: str, value: object) -> object:
    """`validate` for a value already in a database: also what an earlier version allowed to be saved."""
    try:
        return validate(key, value)
    except ValueError:
        if key not in _EARLIER_RULES:
            raise
        return _EARLIER_RULES[key](value)


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


def ha_configured(session: Session) -> bool:
    """Push messages are switched on and everything they need is entered (Spec 10.6)."""
    return bool(
        get_setting(session, "ha_enabled")
        and get_setting(session, "ha_url")
        and get_setting(session, "ha_token")
        and get_setting(session, "ha_service")
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
    """Do a stored and an entered address name the same place?

    Decides whether a stored secret (the Bambuddy key, the Home Assistant token, Spec 10.6
    "Sicherheit", 11.5a) may be sent to an address: it belongs to one address and never travels to
    another. Written differently is the same: case of scheme and host, a default port left out, a
    trailing slash. Everything else counts, the path too (p159): behind a proxy that routes by path,
    another path can be another service. Query, fragment and user part count as well - in doubt the
    secret is typed again, never sent too far. Rejected: one comparison per secret (until v0.3.12 the
    token compared the path and the key did not; two rules for one question drift apart).
    """
    if not a or not b:
        return False

    def place(url: str) -> tuple[object, ...]:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        default = {"http": 80, "https": 443}.get(scheme)
        return (
            scheme,
            (parts.hostname or "").lower(),
            parts.port or default,
            parts.path.rstrip("/"),
            parts.query,
            parts.fragment,
            parts.username,
            parts.password,
        )

    try:
        return place(a) == place(b)
    except ValueError:  # an invalid port or address is never "the same"
        return False
