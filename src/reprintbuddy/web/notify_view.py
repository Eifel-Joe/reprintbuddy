"""What the settings tab "Notifications" shows (Spec 10.6): the stored values, the mask of the token,
the tick boxes of the events and the status of the last message (in memory, `notify.service`)."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from reprintbuddy import settings as st

# The tone of an outcome in the status card and after the test message; any other is "bad".
OUTCOME_TONE = {"accepted": "ok", "unclear": "warn"}


def stored_values(db: Session) -> dict[str, object]:
    """The fields of the card as stored; a page shown again after an error overrides them with
    what was typed (never the token)."""
    return {
        "ha_enabled": bool(st.get_setting(db, "ha_enabled")),
        "ha_url": st.get_setting(db, "ha_url") or "",
        "ha_service": st.get_setting(db, "ha_service") or "",
        "ha_names": bool(st.get_setting(db, "ha_names")),
        "ha_events": list(st.get_setting(db, "ha_events")),  # type: ignore[arg-type]
    }


def settings_context(request: Request, db: Session) -> dict[str, object]:
    notifier = getattr(request.app.state, "notifier", None)
    return {
        "ha_token_mask": st.display_value("ha_token", st.get_setting(db, "ha_token")),
        "ha_event_keys": list(st.HA_EVENTS),
        "notify_status": notifier.status if notifier is not None else None,
        "outcome_tone": OUTCOME_TONE,
    }


def problem(request: Request) -> str | None:
    """The outcome behind the warning "Home Assistant not reachable" (Spec 10.2), or None."""
    notifier = getattr(request.app.state, "notifier", None)
    return notifier.status.problem if notifier is not None else None
