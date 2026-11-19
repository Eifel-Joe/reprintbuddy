"""The zone of the browser, reported by the page in a cookie (Spec 10.5a, 1.2.2).

static/js/zone.js writes the browser's zone into the cookie `rb_zone` on every page, the sign-in and
the setup pages included. `BrowserZone` reads it for every request and shows the whole request in it
(`localtime.shown_in`): the page, a fetch of its regions (the same request again, Spec 10.5.3), the
answer to a form, an error page. Without a valid cookie the zone of the server holds, as before 1.2.2.
The page names the zone it was computed in (`data-zone` on `<html>`); zone.js loads it once more when
that is not the browser's zone.

The check (Spec 10.5a "Sicherheit"): a name from the IANA list (`zoneinfo.available_timezones()`, read
once when this module is imported at start) of at most 64 characters. Anything else counts as not
reported: no error page, no line in any log, and the value is never written anywhere. A forged cookie
only moves the times in its own browser. `ZoneInfo(name)` alone would read a file for a name like
`../../etc/passwd`; the list keeps every name it was not built from away from it.
Rejected: answering 400 (a browser whose zone the server's tzdata does not know yet could not open a
page at all), UTC as the fallback (without JavaScript the pages would change from what they were).
Tests: tests/web/test_browser_zone.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from reprintbuddy import localtime

COOKIE = "rb_zone"
MAX_LENGTH = 64
# Zone files of the system that are no IANA names. Root cause (CI on Linux, 1.2.2): Debian and Ubuntu put
# the link `/usr/share/zoneinfo/localtime -> /etc/localtime` into the zone folder, and
# `available_timezones()` lists every zone file it finds there, so "localtime" was taken. Rejected: only the
# names of the pip package tzdata (a system with newer zone data would lose names its browsers report).
NOT_ZONES = frozenset({"localtime", "posixrules"})


def known_zones(listed: Iterable[str]) -> frozenset[str]:
    """The names a browser may report: what the zone data lists, without the entries of the system."""
    return frozenset(listed) - NOT_ZONES


KNOWN_ZONES: frozenset[str] = known_zones(available_timezones())
LOCALTIME_LINK = "/etc/localtime"  # the link `timedatectl set-timezone` sets


def reported_zone(value: str | None) -> ZoneInfo | None:
    """The zone a cookie names, None when it names none of the list."""
    if not value or len(value) > MAX_LENGTH or value not in KNOWN_ZONES:
        return None
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):  # listed, but unreadable: as if not reported
        return None


class BrowserZone:
    """ASGI middleware: the request is shown in the zone its cookie reports (`localtime.shown_in`).

    Pure ASGI, around the whole application: the gate's error pages, the routes, the error handlers and
    the tasks the `call_next` middlewares start for them all inherit the zone, and it ends with the request.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with localtime.shown_in(reported_zone(HTTPConnection(scope).cookies.get(COOKIE))):
            await self.app(scope, receive, send)


def _name_of(zone: tzinfo) -> str:
    return getattr(zone, "key", None) or zone.tzname(datetime.now(UTC)) or "UTC"


def server_zone_name() -> str:
    """The name of the zone of the server, for the settings page and `data-zone`.

    The IANA name where it can be told: the pinned zone, `TZ` of the service, or the zone file
    `/etc/localtime` points to (`timedatectl set-timezone` sets that link); otherwise the short name of
    the machine's zone now ("UTC", "CET"). A name that is not the browser's makes zone.js load a page
    of a browser without the cookie once more; that is all.
    """
    if localtime.SERVER_ZONE is not None:
        return _name_of(localtime.SERVER_ZONE)
    named = os.environ.get("TZ", "").lstrip(":")
    if named in KNOWN_ZONES:
        return named
    target = os.path.realpath(LOCALTIME_LINK).replace(os.sep, "/")
    if "/zoneinfo/" in target and (linked := target.split("/zoneinfo/", 1)[1]) in KNOWN_ZONES:
        return linked
    return datetime.now(UTC).astimezone().tzname() or "UTC"


def page_zone_name() -> str:
    """The zone this request is shown in, by name: the browser's, else the server's."""
    zone = localtime.browser_zone()
    return _name_of(zone) if zone is not None else server_zone_name()
