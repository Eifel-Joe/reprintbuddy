"""Messages for the next page shown (Post/Redirect/Get), with parameters.

The one queue of messages (since v0.3.13 the settings page uses it too): a list of
(key, params, warning) under its own session key, shown by the layout in one box that
static/js/flash.js looks after (good news goes away by itself, trouble stays until dismissed).
"""

from __future__ import annotations

from typing import NamedTuple

from fastapi import Request

from reprintbuddy.web.i18n import translate

FLASHES_KEY = "flashes"
MAX_FLASHES = 8  # the whole list lives in the (size limited) session cookie


class FlashMessage(NamedTuple):
    text: str
    warning: bool  # trouble, not success: shown in the look of an error

    def __str__(self) -> str:
        return self.text


def flash(request: Request, key: str, *, warning: bool = False, **params: object) -> None:
    """Queue a message for the next page; `warning` for anything that is not good news."""
    flashes = list(request.session.get(FLASHES_KEY, []))
    flashes.append([key, {k: v for k, v in params.items()}, warning])
    request.session[FLASHES_KEY] = flashes[-MAX_FLASHES:]


def pop_flashes(request: Request) -> list[FlashMessage]:
    """The pending messages in the interface language; the list is emptied."""
    from reprintbuddy.web.deps import current_language

    pending = request.session.pop(FLASHES_KEY, None)
    if not pending:
        return []
    lang = current_language(request)
    # A message queued by an older version has no third entry: it is a plain message.
    return [FlashMessage(translate(lang, item[0], **item[1]), bool(item[2]) if len(item) > 2 else False) for item in pending]
