"""The zones times are shown and days are counted in, for the pages and the domain alike.

Times are stored as naive UTC. Two zones (Spec 10.5a, 1.2.2):

- The zone of the display: the zone of the browser that asked for the page, set for one request from
  its cookie (`web.zone.BrowserZone` calls `shown_in`); without a valid cookie, and outside a request
  (the engine, the command line, the domain in the tests), the zone of the server. `to_local`,
  `local_date`, `at_local_time`, `end_of_local_day` and `booked_day` read it: everything a page shows,
  and the days of a booking typed in that browser (the date field's "today", the end of the day of an
  inventory dated back, p260). A form with a date carries the zone its page was computed in, and its
  answer is computed in that one (`web.zone.zone_of_form`). Only the web layer uses these helpers
  (tests/test_zone_helpers_guard.py): a task started in a request keeps the request's zone.
- The zone of the server (p183, v0.3.14: the zone the machine is set to, `timedatectl set-timezone` or
  `TZ` for the service): `to_server_local`, `server_date`, `start_of_server_day`, `end_of_server_day`.
  For the rules that compare stored days and also run without a browser (the day of marking, Spec 9.4;
  old counts dated back, p301; the days of the filament record and of the usage, Spec 9.3, p719): their
  result must not depend on which device happened to look first.

Root cause (1.2.2): one module variable held the zone for the whole process. A server in UTC showed
every time an hour or two off to a browser in Berlin, and between midnight and 1 o'clock (2 in summer)
its date field still offered the day before as "today". Fix: a ContextVar holds the browser's zone for
the request being answered; tasks and worker threads of that request inherit it, other requests never
see it. Rejected: the zone as a parameter through every view, filter and domain message (dozens of
places, and one forgotten shows the server's zone again); formatting the times in the browser with
JavaScript (the texts are put together on the server, a label is an image, and the date field and the
end of a day need the zone on the server all the same).

Tests pin the zone of the server through `SERVER_ZONE` (`conftest.fixed_display_zone`) and set a
browser's with `shown_in`. Tests: tests/test_localtime.py, tests/web/test_server_time_zone.py,
tests/web/test_browser_zone.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, time, timedelta, tzinfo

SERVER_ZONE: tzinfo | None = None  # `astimezone(None)`: the local zone of the machine, at that moment; tests pin one

# The zone of the browser whose request is being answered; None: none reported (or not a valid one), or
# no request at all. Only `shown_in` sets it.
_browser_zone: ContextVar[tzinfo | None] = ContextVar("reprintbuddy_browser_zone", default=None)


def browser_zone() -> tzinfo | None:
    """The zone the browser of this request reported, None without one."""
    return _browser_zone.get()


def display_zone() -> tzinfo | None:
    """The zone of the display: the browser's, else the server's (None: the machine's)."""
    zone = _browser_zone.get()
    return SERVER_ZONE if zone is None else zone


@contextmanager
def shown_in(zone: tzinfo | None) -> Iterator[None]:
    """Show and count in `zone` until the block ends (one request); None: the zone of the server."""
    token = _browser_zone.set(zone)
    try:
        yield
    finally:
        _browser_zone.reset(token)


def _in_zone(dt: datetime, zone: tzinfo | None) -> datetime:
    if dt.tzinfo is None:  # stored naive, meaning UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone)


def _at(day: date, clock: time, zone: tzinfo | None) -> datetime:
    """The moment `clock` on `day` in `zone`, as naive UTC like every stored time.

    Taken through the zone itself with the offset of that day, not of today. Review finding: a
    booking dated back took today's clock time with today's offset; with the zone of the machine
    that is a fixed offset, so a sale typed at 23:30 in winter and dated to a summer day landed on
    the next day, and the check against a later count (p261) looked at the wrong day.
    """
    moment = datetime.combine(day, clock.replace(tzinfo=None))
    local = moment.astimezone() if zone is None else moment.replace(tzinfo=zone)
    return local.astimezone(UTC).replace(tzinfo=None)


def to_local(dt: datetime) -> datetime:
    """A naive datetime means UTC; the result is in the zone of the display (`display_zone`)."""
    return _in_zone(dt, display_zone())


def local_date(dt: datetime) -> date:
    """The calendar day a stored time belongs to, in the zone of the display."""
    return to_local(dt).date()


def at_local_time(day: date, clock: time) -> datetime:
    """The moment `clock` on `day` in the zone of the display, as naive UTC (see `_at`)."""
    return _at(day, clock, display_zone())


def end_of_local_day(day: date) -> datetime:
    """The last moment of `day` in the zone of the display (p260), as naive UTC; a day with a change
    of summer time ends at its own 23:59:59.999999."""
    return _at(day, time.max, display_zone())


def to_server_local(dt: datetime) -> datetime:
    """`to_local` in the zone of the server, whatever the browser of the request reported."""
    return _in_zone(dt, SERVER_ZONE)


def server_date(dt: datetime) -> date:
    """The calendar day of a stored time in the zone of the server."""
    return to_server_local(dt).date()


def end_of_server_day(day: date) -> datetime:
    """`end_of_local_day` in the zone of the server."""
    return _at(day, time.max, SERVER_ZONE)


def start_of_server_day(day: date) -> datetime:
    """The first moment of `day` in the zone of the server, as naive UTC (p719: the 30 days of a part's
    usage)."""
    return _at(day, time.min, SERVER_ZONE)


DAY_END_MICROSECOND = 999_999  # `time.max`: a count dated back is stored at the end of its day (p260)
_NOON = time(12, 0)


def booked_day(moment: datetime) -> date:
    """The day a booking is shown on, in the zone of the display.

    The midnight rule (Spec 10.5a "Tage beim Buchen"): an inventory dated back is stored at the end of
    its day in the zone it was booked in (`end_of_local_day`, the only stored times with 999999
    microseconds, see `stock._holds_until`). In another zone that moment may be early on the next day:
    the end of 18 November in UTC is 00:59 on the 19th in Berlin. Such a moment shows the day before the
    nearest midnight of the display zone, which is the day that was booked for zones less than 12 hours
    apart. At exactly 12 hours it fails one way (review finding 4): the end of a day shown 12 hours to the
    west is 11:59:59.999999 of that day, 12 hours to the east 11:59:59.999999 of the next, the same clock
    time, so no rule can tell the two apart (Spec 10.5a: "weniger als 12 Stunden").
    Any other moment shows its own day.
    Rejected: storing the booked day as a date of its own (a new column and a migration, later as a
    point of its own); the zone of the server for these days (a browser elsewhere would see its own
    bookings on the wrong day).
    """
    local = to_local(moment)
    if moment.microsecond != DAY_END_MICROSECOND:
        return local.date()
    return local.date() if local.time() >= _NOON else local.date() - timedelta(days=1)
