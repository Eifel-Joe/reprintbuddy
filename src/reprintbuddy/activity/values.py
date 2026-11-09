"""What the activity log may say about a setting or a field (Spec 10.7, 11.5a).

Every setting is either shown (with the kind of its value) or internal. Shown means: numbers,
switches and the language old -> new; an address only with scheme, host and port; a host name as
it is; sensitive data only "changed"/"removed"; the certificate only "uploaded"/"removed"; lists and
free texts only "changed". Nothing else of a value ever reaches the log.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

ValueKind = Literal[
    "number", "switch", "switch_inverted", "language", "address", "host", "secret", "certificate", "list", "text"
]

SETTINGS: dict[str, ValueKind] = {
    "language": "language",
    "poll_minutes": "number",
    "queue_cap": "number",
    "insert_next": "switch",
    "automation": "switch",
    # Stored as "queue running", asked on the page as "stop the queue": shown as on the page.
    "queue_running": "switch_inverted",
    "update_check": "switch",
    "qr_base_url": "address",
    "label_printer_host": "host",
    "label_roll": "text",
    "label_fields": "list",
    "bambuddy_url": "address",
    "bambuddy_username": "text",
    "bambuddy_api_key": "secret",
    "host": "host",
    "port": "number",
    "tls_cert": "certificate",
    "tls_key": "secret",
    "ha_enabled": "switch",
    "ha_url": "address",
    "ha_token": "secret",
    "ha_service": "text",
    "ha_names": "switch",
    "ha_events": "list",
}

# The name a setting has in the log and its texts (`activity.setting.<name>`). The storage key of a
# secret never appears in the log, not even as a name: the export proves it is free of them by looking
# for those keys (tests/ops/test_export.py).
NAMES: dict[str, str] = {key: key for key in SETTINGS} | {
    "bambuddy_api_key": "bambuddy_key",
    "tls_cert": "https_certificate",
    "tls_key": "https_key",
    "ha_token": "ha_access_token",
}

# What the log says of a value it does not show (secrets, the certificate, lists, texts).
CHANGES: tuple[str, ...] = ("changed", "removed", "uploaded")

# Kept by Reprintbuddy for itself; no settings page shows them.
INTERNAL: frozenset[str] = frozenset(
    {"session_secret", "ha_since", "bambuddy_review_confirmed", "ha_token_rejected"}  # the last: p152, set by the sender
)

# Columns of the edit forms (master data) -> the field the log names. Several columns can be one
# field ("print target"); the texts are `activity.field.<field>`.
FIELDS: dict[str, str] = {
    "name": "name",
    "sku": "sku",
    "label_text": "label_text",
    "units_per_print": "units_per_print",
    "min_stock": "min_stock",
    "buffer": "buffer",
    "target_model": "target",
    "printer_id": "target",
    "needs_assembly": "needs_assembly",
    "filaments": "filaments",
    "parts": "parts",
    "unit": "unit",
    "pack_size": "pack_size",
    "min_order_packs": "min_order_packs",
    "lead_time_days": "lead_time_days",
    "safety_value": "safety",
    "safety_unit": "safety",
    "supplier": "supplier",
    "order_url": "order_url",
    "positions": "positions",
}


def clean_address(value: object) -> str | None:
    """`scheme://host[:port]` of an http(s) address; anything else None. No user, password, path,
    query or fragment: an address may carry a key in them."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def _plain(kind: ValueKind, value: object) -> object:
    if kind == "address":
        return clean_address(value)
    if kind == "host":
        return value if isinstance(value, str) else None
    if kind == "number":
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if kind in ("switch", "switch_inverted"):
        flag = bool(value)
        return (not flag) if kind == "switch_inverted" else flag
    if kind == "language":
        return value if isinstance(value, str) else None
    raise AssertionError(kind)  # pragma: no cover - the callers below never get here


def shown_change(key: str, old: object, new: object) -> dict[str, object]:
    """What the log keeps of the change of setting `key` from `old` to `new`."""
    kind, name = SETTINGS[key], NAMES[key]
    if kind == "secret":
        return {"setting": name, "change": "removed" if new is None else "changed"}
    if kind == "certificate":
        return {"setting": name, "change": "removed" if new is None else "uploaded"}
    if kind in ("list", "text"):
        return {"setting": name, "change": "changed"}
    before, after = _plain(kind, old), _plain(kind, new)
    if before == after:  # e.g. an address changed only behind scheme, host and port
        return {"setting": name, "change": "changed"}
    return {"setting": name, "old": before, "new": after}
