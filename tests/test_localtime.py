"""The calendar day of the display zone for the domain (p260, p262): one source for the zone.

Times are stored as naive UTC. Which day a time belongs to, and when a day ends, is decided in the
zone the times are shown in (p183) - by the web layer and by the domain alike.
"""

from datetime import UTC, date, datetime, time

import pytest

from reprintbuddy import localtime
from reprintbuddy.web import formatting


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


def test_the_zone_of_the_server_by_default(monkeypatch):
    assert localtime.SERVER_ZONE is None  # `astimezone(None)`: the zone the machine is set to
    monkeypatch.setattr(localtime, "LOCAL_ZONE", localtime.SERVER_ZONE)
    end = localtime.end_of_local_day(date(2026, 7, 15))
    assert localtime.to_local(end).replace(tzinfo=None) == datetime(2026, 7, 15, 23, 59, 59, 999999)


def test_the_display_reads_the_same_zone(monkeypatch):
    """One source: setting the zone in `localtime` changes what the pages show."""
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(localtime, "LOCAL_ZONE", ZoneInfo("America/New_York"))
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


def test_a_clock_time_on_a_day_in_the_zone_of_the_server(monkeypatch):
    """Review finding: with the zone of the machine, the offset of today was used for an earlier
    day; a sale typed at 23:30 in winter and dated to a summer day landed on the next day."""
    monkeypatch.setattr(localtime, "LOCAL_ZONE", localtime.SERVER_ZONE)
    for day in (date(2026, 7, 15), date(2026, 1, 15)):
        moment = localtime.at_local_time(day, time(23, 30))
        assert localtime.to_local(moment).replace(tzinfo=None) == datetime.combine(day, time(23, 30))
