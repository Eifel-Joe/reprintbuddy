"""Connection check for the setup page and the settings page.

Reprintbuddy asks Bambuddy who owns the key and which rights the key can really
use (`GET /api/v1/auth/me`, read-only). The key is only ever sent in the header
and never logged.
"""

from __future__ import annotations

from contextlib import nullcontext
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
    "filaments:read",  # to tell which spool management Bambuddy runs (Spec 9.1)
    # Bambuddy's default print options, sent with every entry (p342, 0.4.0, Spec 7.3):
    # `GET /settings/` needs it. Until 0.3.x it was needed only to create the key.
    "settings:read",
)

# Rights that are not required: without them only one feature is missing, the check still
# saves and no notice appears after an update (Spec 5.1a, 11.3 a–c). Uploading print files
# needs `library:upload` in the group and "Manage Library" at the key (Bambuddy
# `core/auth.py`: LIBRARY_UPLOAD -> can_manage_library); `/auth/me` lists the right only when
# both allow it.
UPLOAD_PERMISSION = "library:upload"
# Bambuddy's live channel (p170, Spec 7.6, 11.3, 14): `POST /auth/ws-token` needs this right in the
# group; at the key "Read Status" covers it (Bambuddy `core/auth.py`: WEBSOCKET_CONNECT -> can_read_status).
# Without it Reprintbuddy only asks at the set interval: recommended, not required.
LIVE_PERMISSION = "websocket:connect"
# Withdrawing prints after a cancel (Spec 7.2a, 11.3, 0.4.1 p382): `DELETE /queue/{id}` needs this right
# in the group (Bambuddy `print_queue.py:2701-2706`); with only "own" it may delete the entries of the
# key's owner, which are Reprintbuddy's. At the key "Manage Queue" covers it (`core/auth.py`:
# QUEUE_DELETE_OWN -> can_queue). Without it a cancel only shrinks Reprintbuddy's own queue.
WITHDRAW_PERMISSION = "queue:delete_own"
_WITHDRAW_ALL = "queue:delete_all"  # an administrator's key may delete every entry, own ones included
OPTIONAL_PERMISSIONS: tuple[str, ...] = (UPLOAD_PERMISSION, LIVE_PERMISSION, WITHDRAW_PERMISSION)

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
    # Bambuddy has special labels for a few rights (groups.py): this is the one for the right
    # to print without review.
    "queue:start_unreviewed": "Print Without Review (else jobs wait for someone to start them)",
    "printers:read": "Read Printers",
    "inventory:read": "Read Inventory",
    "filaments:read": "Read Filaments",
    "settings:read": "Read Settings",
    UPLOAD_PERMISSION: "Upload Library",  # optional (Spec 5.1a)
    LIVE_PERMISSION: "Connect Websocket",  # optional (p170); Bambuddy makes the label from the name
    WITHDRAW_PERMISSION: "Delete Own Queue",  # optional (p382); made from the name as well
}

# Needed only while the key is created (the user must open the API keys page and create a key).
# Both may be removed from the group again afterwards.
# Reprintbuddy does not check them: its own key does not need them. `settings:read` (without it
# Bambuddy hides the whole Settings menu) was the third one until 0.4.0; it is required now.
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
    "filaments:read": "status",
    "settings:read": "status",  # Bambuddy `core/auth.py`: SETTINGS_READ -> can_read_status
    "queue:create": "queue",
    "queue:start_unreviewed": "queue",
    UPLOAD_PERMISSION: "library",  # "Manage Library"
    LIVE_PERMISSION: "status",  # "Read Status"
    WITHDRAW_PERMISSION: "queue",  # "Manage Queue"
}

# The settings page 3 of the setup asks for: (id, Bambuddy's English label, on or off).
# "Manage Library" is on since v0.3.9 (Spec 11.3 b): at the key it works only as far as the
# group allows, and the group gets only `library:upload` (no update, delete or slicing).
KEY_SETTINGS_ON: tuple[tuple[str, str], ...] = (
    ("status", "Read Status"),
    ("queue", "Manage Queue"),
    ("library", "Manage Library"),
)
KEY_SETTINGS_OFF: tuple[tuple[str, str], ...] = (
    ("control", "Control Printer"),
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

    @property
    def can_upload(self) -> bool:
        """The key may upload print files (optional right, Spec 5.1a)."""
        return UPLOAD_PERMISSION in self.granted

    @property
    def can_live(self) -> bool:
        """The key may open Bambuddy's live channel (optional right, p170)."""
        return LIVE_PERMISSION in self.granted

    @property
    def can_withdraw(self) -> bool:
        """The key may delete Reprintbuddy's waiting entries after a cancel (optional right, p382)."""
        return WITHDRAW_PERMISSION in self.granted or _WITHDRAW_ALL in self.granted


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
    """Can Reprintbuddy reach Bambuddy, and may this key do everything Reprintbuddy needs?

    A thin wrapper around `HttpBambuddyClient.check_connection`; the caller's `client` stays open.
    """
    # Imported here: `client` itself imports from this module.
    from reprintbuddy.bambuddy.client import HttpBambuddyClient

    return await HttpBambuddyClient(url, api_key, lambda: nullcontext(client)).check_connection()
