"""The calendar day in the zone the times are shown in, for the pages and the domain alike.

Times are stored as naive UTC. Which day a time belongs to and when a day ends is decided in the
zone of the server (p183, v0.3.14: the zone the machine is set to, `timedatectl set-timezone` or
`TZ` for the service). p260: an inventory dated back holds for the end of that day, and the domain
needs the same zone as the display (p262 too). So the zone lives here, once; `web.formatting`
reads it from here. Tests pin a zone through `LOCAL_ZONE` (`conftest.fixed_display_zone`).
Tests: tests/test_localtime.py, tests/web/test_server_time_zone.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo

SERVER_ZONE: tzinfo | None = None  # `astimezone(None)`: the local zone of the machine, at that moment
LOCAL_ZONE: tzinfo | None = SERVER_ZONE  # the zone times are shown in; tests pin one


def to_local(dt: datetime) -> datetime:
    """A naive datetime means UTC; the result is in the zone of the server (`LOCAL_ZONE`)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LOCAL_ZONE)


def local_date(dt: datetime) -> date:
    """The calendar day a stored time belongs to, in the zone of the display."""
    return to_local(dt).date()


def at_local_time(day: date, clock: time) -> datetime:
    """The moment `clock` on `day` in the zone of the display, as naive UTC like every stored time.

    Taken through the zone itself with the offset of that day, not of today. Review finding: a
    booking dated back took today's clock time with today's offset; with the zone of the machine
    that is a fixed offset, so a sale typed at 23:30 in winter and dated to a summer day landed on
    the next day, and the check against a later count (p261) looked at the wrong day.
    """
    moment = datetime.combine(day, clock.replace(tzinfo=None))
    local = moment.astimezone() if LOCAL_ZONE is None else moment.replace(tzinfo=LOCAL_ZONE)
    return local.astimezone(UTC).replace(tzinfo=None)


def end_of_local_day(day: date) -> datetime:
    """The last moment of `day` in the zone of the display (p260), as naive UTC; a day with a change
    of summer time ends at its own 23:59:59.999999."""
    return at_local_time(day, time.max)
