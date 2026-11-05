"""Shared by setup step 2 and the settings page: validate the input, ask Bambuddy."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.bambuddy.connection import (
    EDITOR_LABELS,
    KEY_CREATION_LABELS,
    KEY_RIGHT_FOR,
    KEY_SETTINGS_OFF,
    KEY_SETTINGS_ON,
    REQUIRED_PERMISSIONS,
    ConnectionCheck,
    check_connection,
)
from reprintbuddy.web.deps import release_connection


def validate_address(address: str) -> tuple[str | None, str | None]:
    """(normalised address, None) or (None, message key)."""
    try:
        return str(st.validate("bambuddy_url", address)), None
    except ValueError:
        return None, "bambuddy.invalid_url"


def validate_key(key: str) -> tuple[str | None, str | None]:
    """(stripped key, None) or (None, message key)."""
    try:
        return str(st.validate("bambuddy_api_key", key)), None
    except ValueError:
        return None, "bambuddy.key_invalid"


async def run_check(request: Request, db: Session, url: str, api_key: str) -> ConnectionCheck:
    release_connection(db)  # nothing is pending; do not hold a connection while Bambuddy answers
    async with request.app.state.http_client_factory() as client:
        return await check_connection(url, api_key, client)


def result_context(check: ConnectionCheck | None) -> dict[str, object]:
    """What the result partial needs."""
    return {"check": check, **permission_context()}


def permission_context() -> dict[str, object]:
    """The fixed tables the templates need (labels, where a right can be set)."""
    return {
        "required_permissions": REQUIRED_PERMISSIONS,
        "key_creation_permissions": tuple(KEY_CREATION_LABELS),
        "editor_labels": {**EDITOR_LABELS, **KEY_CREATION_LABELS},
        "key_right_for": KEY_RIGHT_FOR,
        "key_settings_on": KEY_SETTINGS_ON,
        "key_settings_off": KEY_SETTINGS_OFF,
    }
