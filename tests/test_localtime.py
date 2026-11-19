"""The zone of the display and the zone of the server (p260, p262, Spec 10.5a).

Times are stored as naive UTC. A page shows them, and counts the days of a booking typed in it, in the
zone of the browser that asked for it (1.2.2, `shown_in` for one request); without one, and outside a
request, in the zone of the server. Rules that compare stored days keep the zone of the server whoever
asks (Spec 10.5a "Ohne Browser"). The tests pin the zone of the server to Europe/Berlin (conftest).
"""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from reprintbuddy import localtime
from reprintbuddy.web import formatting

NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")


@pytest.mark.parametrize(
    ("day", "utc"),
    [
        (date(2026, 1, 15), datetime(2026, 1, 15, 22, 59, 59, 999999)),  # winter, UTC+1
        (date(2026, 7, 15), datetime(2026, 7, 15, 21, 59, 59, 999999)),  # summer, UTC+2
        (date(2026, 3, 29), datetime(2026, 3, 29, 21, 59, 59, 999999)),  # clocks go forward that day
        (date(2026, 10, 25), datetime(2026, 10, 25, 22, 59, 59, 999999)),  # clocks go back that day
    ],
)
def test_the_end_of_a_day_in_the_zone_of_the_display(day, utc):  # conftest pins Europe/Berlin
    assert localtime.end_of_local_day(day) == utc  # naive UTC, like every stored time
    assert localtime.local_date(utc) == day
    assert localtime.to_local(utc).time() == time(23, 59, 59, 999999)


@pytest.mark.parametrize(
    ("stored", "day"),
    [
        (datetime(2026, 7, 15, 21, 59), date(2026, 7, 15)),  # 23:59 in Berlin
        (datetime(2026, 7, 15, 22, 0), date(2026, 7, 16)),  # midnight in Berlin, still the 15th in UTC
        (datetime(2026, 7, 15, 22, 0, tzinfo=UTC), date(2026, 7, 16)),  # an aware time works the same
    ],
)
def test_the_local_date_of_a_stored_time(stored, day):
    assert localtime.local_date(stored) == day


def test_the_zone_of_the_machine_when_the_server_zone_is_not_pinned(monkeypatch):
    monkeypatch.setattr(localtime, "SERVER_ZONE", None)  # `astimezone(None)`: the zone the machine is set to
    end = localtime.end_of_local_day(date(2026, 7, 15))
    assert localtime.to_local(end).replace(tzinfo=None) == datetime(2026, 7, 15, 23, 59, 59, 999999)


def test_the_display_reads_the_same_zone(monkeypatch):
    """One source: setting the zone of the server in `localtime` changes what the pages show without a browser."""
    monkeypatch.setattr(localtime, "SERVER_ZONE", NEW_YORK)
    assert formatting.format_local(datetime(2026, 7, 15, 12, 0), "en") == "2026-07-15 08:00"


@pytest.mark.parametrize(
    ("day", "clock", "utc"),
    [
        (date(2026, 10, 24), time(23, 30), datetime(2026, 10, 24, 21, 30)),  # a summer day, UTC+2
        (date(2026, 11, 10), time(23, 30), datetime(2026, 11, 10, 22, 30)),  # a winter day, UTC+1
    ],
)
def test_a_clock_time_on_a_day_takes_the_offset_of_that_day(day, clock, utc):
    assert localtime.at_local_time(day, clock) == utc


def test_a_clock_time_on_a_day_in_the_zone_of_the_machine(monkeypatch):
    """Review finding: with the zone of the machine, the offset of today was used for an earlier
    day; a sale typed at 23:30 in winter and dated to a summer day landed on the next day."""
    monkeypatch.setattr(localtime, "SERVER_ZONE", None)
    for day in (date(2026, 7, 15), date(2026, 1, 15)):
        moment = localtime.at_local_time(day, time(23, 30))
        assert localtime.to_local(moment).replace(tzinfo=None) == datetime.combine(day, time(23, 30))


# ---- 1.2.2 (Spec 10.5a): the zone of the browser, for one request ------------------------------------


def test_a_page_shows_the_zone_of_its_browser():
    noon_utc = datetime(2026, 7, 15, 12, 0)
    with localtime.shown_in(NEW_YORK):
        assert formatting.format_local(noon_utc, "en") == "2026-07-15 08:00"
        assert localtime.display_zone() is NEW_YORK and localtime.browser_zone() is NEW_YORK
    assert formatting.format_local(noon_utc, "en") == "2026-07-15 14:00"  # after it: the server's (Berlin)
    assert localtime.browser_zone() is None


def test_no_browser_zone_means_the_zone_of_the_server():
    with localtime.shown_in(None):
        assert localtime.display_zone() is localtime.SERVER_ZONE
        assert localtime.end_of_local_day(date(2026, 7, 15)) == datetime(2026, 7, 15, 21, 59, 59, 999999)


def test_the_days_of_a_booking_follow_the_browser():
    """The date field's "today", the end of a day of an inventory dated back and the clock time of a
    booking dated back are counted in the zone of the browser that books (Spec 10.5a, 6.2)."""
    with localtime.shown_in(NEW_YORK):
        assert localtime.end_of_local_day(date(2026, 7, 15)) == datetime(2026, 7, 16, 3, 59, 59, 999999)
        assert localtime.at_local_time(date(2026, 7, 15), time(20, 0)) == datetime(2026, 7, 16, 0, 0)
        assert localtime.local_date(datetime(2026, 7, 16, 1, 0)) == date(2026, 7, 15)


def test_two_requests_at_once_keep_their_own_zone():
    """A ContextVar, not a module variable: the page of one browser is never drawn in the zone of
    another browser whose request runs at the same time."""
    noon_utc = datetime(2026, 7, 15, 12, 0)

    async def page(zone):
        with localtime.shown_in(zone):
            first = formatting.format_local(noon_utc, "en")
            await asyncio.sleep(0)  # the other request runs in between
            await asyncio.sleep(0)
            return first, formatting.format_local(noon_utc, "en")

    async def both():
        return await asyncio.gather(page(NEW_YORK), page(TOKYO))

    assert asyncio.run(both()) == [
        ("2026-07-15 08:00", "2026-07-15 08:00"),
        ("2026-07-15 21:00", "2026-07-15 21:00"),
    ]


def test_the_rules_of_the_server_ignore_the_browser():
    """Days compared by rules that also run without a browser (Spec 9.4, p301) stay in the zone of
    the server, whoever looks: the result must not depend on which device checked first."""
    late_evening_utc = datetime(2026, 7, 15, 20, 0)  # 22:00 in Berlin, 05:00 next day in Tokyo
    with localtime.shown_in(TOKYO):
        assert localtime.local_date(late_evening_utc) == date(2026, 7, 16)
        assert localtime.server_date(late_evening_utc) == date(2026, 7, 15)
        assert localtime.to_server_local(late_evening_utc).utcoffset() == timedelta(hours=2)
        assert localtime.end_of_server_day(date(2026, 7, 15)) == datetime(2026, 7, 15, 21, 59, 59, 999999)


# ---- the midnight rule (Spec 10.5a "Tage beim Buchen") ------------------------------------------------
# An inventory dated back is stored at the end of its day (23:59:59.999999) in the zone it was booked in.
# Shown in another zone, that moment may fall early on the next day; the day shown is the day before the
# nearest midnight, so it stays the day that was booked (up to 12 hours apart).

END_OF_18_NOV_UTC = datetime(2026, 11, 18, 23, 59, 59, 999999)  # booked on a server or browser in UTC
END_OF_18_NOV_BERLIN = datetime(2026, 11, 18, 22, 59, 59, 999999)  # booked in Berlin (UTC+1)


@pytest.mark.parametrize("stored", [END_OF_18_NOV_UTC, END_OF_18_NOV_BERLIN])
@pytest.mark.parametrize(
    "zone",
    [ZoneInfo("Europe/Berlin"), ZoneInfo("UTC"), NEW_YORK, TOKYO, ZoneInfo("Pacific/Honolulu"), ZoneInfo("Asia/Kolkata")],
)
def test_a_day_end_shows_the_day_it_was_booked_for(stored, zone):
    with localtime.shown_in(zone):
        assert localtime.booked_day(stored) == date(2026, 11, 18)


def test_without_the_midnight_rule_berlin_would_show_the_next_day():
    """What the rule is for: the end of 18 November in UTC is 00:59 on the 19th in Berlin."""
    with localtime.shown_in(ZoneInfo("Europe/Berlin")):
        assert localtime.local_date(END_OF_18_NOV_UTC) == date(2026, 11, 19)
        assert localtime.booked_day(END_OF_18_NOV_UTC) == date(2026, 11, 18)


@pytest.mark.parametrize(
    ("stored", "day"),
    [
        (datetime(2026, 11, 18, 23, 30), date(2026, 11, 19)),  # 00:30 in Berlin: a moment, its own day
        (datetime(2026, 11, 18, 23, 59, 59, 999998), date(2026, 11, 19)),
        (datetime(2026, 11, 19, 8, 0), date(2026, 11, 19)),
    ],
)
def test_any_other_moment_shows_its_own_day(stored, day):
    assert localtime.booked_day(stored) == day  # Berlin (conftest)
