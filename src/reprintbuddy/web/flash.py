"""Messages for the next page shown (Post/Redirect/Get), with parameters.

B1's `deps.flash` keeps one translation key for the settings page; this module
keeps a list of (key, params) under its own session key and is shown by the layout.
"""

from __future__ import annotations

from fastapi import Request

from reprintbuddy.web.i18n import translate

FLASHES_KEY = "flashes"
MAX_FLASHES = 8  # the whole list lives in the (size limited) session cookie


def flash(request: Request, key: str, **params: object) -> None:
    flashes = list(request.session.get(FLASHES_KEY, []))
    flashes.append([key, {k: v for k, v in params.items()}])
    request.session[FLASHES_KEY] = flashes[-MAX_FLASHES:]


def pop_flashes(request: Request) -> list[str]:
    """The pending messages in the interface language; the list is emptied."""
    from reprintbuddy.web.deps import current_language

    pending = request.session.pop(FLASHES_KEY, None)
    if not pending:
        return []
    lang = current_language(request)
    return [translate(lang, key, **params) for key, params in pending]
