"""Connection check for the setup page and the settings page.

Reprintbuddy asks Bambuddy who owns the key and which rights the key can really
use (`GET /api/v1/auth/me`, read-only). The key is only ever sent in the header
and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import httpx

Reason = Literal[
    "ok",
    "unreachable",
    "unauthorized",
    "forbidden",
    "not_bambuddy",
    "missing_permissions",
]

TIMEOUT_SECONDS = 10.0

# Rights the key owner needs (Spec 11.3 a). `…:read_own` is not enough: it would show
# only Reprintbuddy's own files and queue entries.
REQUIRED_PERMISSIONS: tuple[str, ...] = (
    "library:read_all",
    "queue:read_all",
    "queue:create",
    "queue:start_unreviewed",
    "printers:read",
    "inventory:read",
)

# Assumption until the first live test shows the names Bambuddy really reports (Spec 14):
# for the two "read all" rights, the older name without the suffix counts as well.
# `…:read_own` never does.
_LEGACY_NAMES: dict[str, str] = {
    "library:read_all": "library:read",
    "queue:read_all": "queue:read",
}

# How Bambuddy's group editor names each right (generated server-side in English only,
# backend/app/api/routes/groups.py `_permission_label`). Shown small below the translated text.
EDITOR_LABELS: dict[str, str] = {
    "library:read_all": "Read All Library",
    "queue:read_all": "Read All Queue",
    "queue:create": "Create Queue",
    "queue:start_unreviewed": "Start Unreviewed Queue",
    "printers:read": "Read Printers",
    "inventory:read": "Read Inventory",
}

# Needed only while the key is created (the user must open the API keys page and create a key).
# Reprintbuddy does not check them: its own key does not need them.
KEY_CREATION_LABELS: dict[str, str] = {
    "api_keys:read": "Read Api Keys",
    "api_keys:create": "Create Api Keys",
}

# The effective rights come from the group and the key together. Which key right
# ("status" = Read Status, "queue" = Manage Queue) can stand in for a right the group lacks.
KEY_RIGHT_FOR: dict[str, str] = {
    "library:read_all": "status",
    "queue:read_all": "status",
    "printers:read": "status",
    "inventory:read": "status",
    "queue:create": "queue",
    "queue:start_unreviewed": "queue",
}

# The settings page 3 of the setup asks for: (id, Bambuddy's English label, on or off).
KEY_SETTINGS_ON: tuple[tuple[str, str], ...] = (
    ("status", "Read Status"),
    ("queue", "Manage Queue"),
)
KEY_SETTINGS_OFF: tuple[tuple[str, str], ...] = (
    ("control", "Control Printer"),
    ("library", "Manage Library"),
    ("inventory", "Manage Inventory"),
    ("maintenance", "Manage Maintenance"),
    ("archives", "Manage Archives"),
    ("projects", "Manage Projects"),
    ("cloud", "Allow Cloud Access"),
    ("electricity", "Update Electricity Price"),
    ("notifications", "Send Notifications"),
)

ADMIN_GROUP = "Administrators"


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    reason: Reason
    owner: str | None = None
    owner_is_admin: bool = False
    granted: frozenset[str] = field(default_factory=frozenset)
    missing: tuple[str, ...] = ()


def _missing(granted: frozenset[str]) -> tuple[str, ...]:
    def has(required: str) -> bool:
        legacy = _LEGACY_NAMES.get(required)
        return required in granted or (legacy is not None and legacy in granted)

    return tuple(required for required in REQUIRED_PERMISSIONS if not has(required))


def _group_names(groups: object) -> list[str]:
    """Groups come as objects with a name or as plain names; anything else is ignored."""
    names: list[str] = []
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, str):
                names.append(group)
            elif isinstance(group, dict) and isinstance(group.get("name"), str):
                names.append(group["name"])
    return names


def _evaluate(body: object) -> ConnectionCheck:
    if not isinstance(body, dict):
        return ConnectionCheck(False, "not_bambuddy")
    owner = body.get("username")
    permissions = body.get("permissions")
    if not isinstance(owner, str) or not owner.strip():
        return ConnectionCheck(False, "not_bambuddy")
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        return ConnectionCheck(False, "not_bambuddy")

    owner = owner.strip()
    granted = frozenset(permissions)
    is_admin = body.get("is_admin") is True or ADMIN_GROUP in _group_names(body.get("groups"))
    missing = _missing(granted)
    if missing:
        return ConnectionCheck(
            False,
            "missing_permissions",
            owner=owner,
            owner_is_admin=is_admin,
            granted=granted,
            missing=missing,
        )
    return ConnectionCheck(True, "ok", owner=owner, owner_is_admin=is_admin, granted=granted)


async def check_connection(url: str, api_key: str, client: httpx.AsyncClient) -> ConnectionCheck:
    """Can Reprintbuddy reach Bambuddy, and may this key do everything Reprintbuddy needs?"""
    try:
        response = await client.get(
            f"{url.rstrip('/')}/api/v1/auth/me",
            headers={"X-API-Key": api_key},
            timeout=TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        return ConnectionCheck(False, "unreachable")

    status = response.status_code
    if status == 401:
        return ConnectionCheck(False, "unauthorized")
    if status == 403:
        return ConnectionCheck(False, "forbidden")
    if status >= 500:
        return ConnectionCheck(False, "unreachable")
    if status == 200:
        try:
            body = response.json()
        except ValueError:
            return ConnectionCheck(False, "not_bambuddy")
        return _evaluate(body)
    return ConnectionCheck(False, "not_bambuddy")
