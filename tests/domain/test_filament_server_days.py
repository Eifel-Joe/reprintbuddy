"""The days of the filament record are the days of the server's zone (Spec 9.3, 10.5a, p719).

Until 1.2.2 the record and the views of the use counted days in UTC: at 0:56 in Berlin "today" still
showed the whole day before. Now the record, "today", the calendar periods and the 30 days of the
suggestions count the calendar days of the server's zone; "today" turns over at its midnight. Rows
recorded up to 1.2.2 stay as they are (UTC days). A server in UTC counts as before.
The tests pin the server's zone to Europe/Berlin (`conftest.fixed_display_zone`).
"""

import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from factories import make_snapshot, make_sort
from reprintbuddy import localtime
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.domain import filament, reorder
from reprintbuddy.models import FilamentSnapshot

# 00:30 on Saturday 21 November 2026 in Berlin (winter time, UTC+1)
AFTER_MIDNIGHT = datetime(2026, 11, 20, 23, 30)


def _days(s):
    return sorted({r.day for r in s.scalars(select(FilamentSnapshot))})


@pytest.mark.parametrize(("now", "day"), [
    (AFTER_MIDNIGHT, date(2026, 11, 21)),
    (datetime(2026, 11, 20, 22, 59), date(2026, 11, 20)),  # 23:59 in Berlin
    (datetime(2026, 12, 31, 23, 30), date(2027, 1, 1)),  # new year in Berlin, old one in UTC
    (datetime(2026, 11, 30, 23, 30), date(2026, 12, 1)),  # new month
    (datetime(2026, 11, 15, 23, 30), date(2026, 11, 16)),  # Sunday in UTC, Monday in Berlin
    # summer time begins on Sunday 29 March 2026 at 01:00 UTC
    (datetime(2026, 3, 28, 23, 30), date(2026, 3, 29)),  # 00:30 CET
    (datetime(2026, 3, 29, 22, 30), date(2026, 3, 30)),  # 00:30 CEST, two hours ahead now
    (datetime(2026, 3, 29, 21, 59), date(2026, 3, 29)),  # 23:59 CEST
    # winter time begins on Sunday 25 October 2026 at 01:00 UTC
    (datetime(2026, 10, 24, 22, 30), date(2026, 10, 25)),  # 00:30 CEST
    (datetime(2026, 10, 25, 22, 59), date(2026, 10, 25)),  # 23:59 CET, one hour ahead again
    (datetime(2026, 10, 25, 23, 0), date(2026, 10, 26)),
])
def test_the_day_of_the_record_is_the_servers(now, day):
    assert filament.record_day(now) == day
    assert filament.record_day(now.replace(tzinfo=UTC)) == day  # an aware time means the same


def test_a_server_in_utc_counts_as_before(monkeypatch):
    monkeypatch.setattr(localtime, "SERVER_ZONE", UTC)
    assert filament.record_day(AFTER_MIDNIGHT) == date(2026, 11, 20)
    assert filament.record_day(datetime(2026, 3, 29, 22, 30)) == date(2026, 3, 29)


async def test_the_tick_after_midnight_records_the_new_day(db, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", brand="Brand A")
    await filament.poll_hook(fake_bambuddy, db, AFTER_MIDNIGHT)
    with db() as s:
        assert _days(s) == [date(2026, 11, 21)]


async def test_the_tick_of_a_server_in_utc_records_the_utc_day(db, fake_bambuddy, monkeypatch):
    monkeypatch.setattr(localtime, "SERVER_ZONE", UTC)
    fake_bambuddy.add_spool("PLA", brand="Brand A")
    await filament.poll_hook(fake_bambuddy, db, AFTER_MIDNIGHT)
    with db() as s:
        assert _days(s) == [date(2026, 11, 20)]


def test_a_spool_archived_after_midnight_lands_on_the_servers_day(db):
    """Bambuddy's `archived_at` is UTC; 23:30 UTC on the 20th is the 21st in Berlin."""
    spool = FakeBambuddy().add_spool("PETG", brand="Brand A", remaining_g=300.0)
    with db.begin() as s:
        filament.record_snapshot(s, [spool], date(2026, 11, 19))
        archived = dataclasses.replace(spool, remaining_g=40.0, archived=True, archived_at=AFTER_MIDNIGHT)
        filament.record_snapshot(s, [archived], date(2026, 11, 22))
    with db() as s:
        rows = [(r.day, r.remaining_g, r.archived) for r in s.scalars(select(FilamentSnapshot).order_by(FilamentSnapshot.day))]
        assert rows == [(date(2026, 11, 19), 300.0, False), (date(2026, 11, 21), 40.0, True)]


async def test_rows_recorded_in_utc_stay_and_count(db, fake_bambuddy):
    """The update from 1.2.2 at 00:30 in Berlin: the row of the last tick before it carries the UTC day
    20 November. The first tick after it writes the 21st and leaves that row alone; "today" counts the
    drop from it, "yesterday" the drop of the 20th."""
    spool = fake_bambuddy.add_spool("PLA", brand="Brand A", remaining_g=900.0)
    with db.begin() as s:
        sort = filament.sync_sorts(s, [spool])[spool.id]
        make_snapshot(s, date(2026, 11, 19), spool.id, sort, 1000.0)
        make_snapshot(s, date(2026, 11, 20), spool.id, sort, 950.0)  # written at 23:20 UTC by 1.2.2
    await filament.poll_hook(fake_bambuddy, db, AFTER_MIDNIGHT)
    with db() as s:
        rows = [(r.day, r.remaining_g) for r in s.scalars(select(FilamentSnapshot).order_by(FilamentSnapshot.day))]
        assert rows == [(date(2026, 11, 19), 1000.0), (date(2026, 11, 20), 950.0), (date(2026, 11, 21), 900.0)]
        views = filament.usage(s, [sort], filament.record_day(AFTER_MIDNIGHT))
        assert views["today"][sort].grams == 50.0
        assert views["yesterday"][sort].grams == 50.0


def test_today_after_midnight_holds_nothing_of_the_day_before(db):
    """The finding of p719: at 0:56 in Berlin "today" showed the 1041 g of the day before."""
    with db.begin() as s:
        sort = make_sort(s).id
        make_snapshot(s, date(2026, 11, 19), 1, sort, 3000.0)
        make_snapshot(s, date(2026, 11, 20), 1, sort, 1959.0)  # 1041 g on the 20th
        make_snapshot(s, date(2026, 11, 21), 1, sort, 1959.0)  # the first tick after midnight
    with db() as s:
        views = filament.usage(s, [sort], filament.record_day(AFTER_MIDNIGHT))
        assert views["today"][sort].grams == 0.0
        assert views["yesterday"][sort].grams == 1041.0


def test_the_suggestions_count_the_servers_days(db, monkeypatch):
    seen = []
    monkeypatch.setattr(reorder, "suggestions", lambda session, **kw: seen.append(kw["today"]) or [])
    with db.begin() as s:
        reorder.current(s, None, AFTER_MIDNIGHT, write=False)
        reorder.current(s, [], AFTER_MIDNIGHT)
    assert seen == [date(2026, 11, 21), date(2026, 11, 21)]


def test_the_reach_on_the_overview_counts_the_servers_days(bb_client, fake_bambuddy, monkeypatch):
    from reprintbuddy.web.routes import overview

    seen = []
    monkeypatch.setattr(overview, "utcnow", lambda: AFTER_MIDNIGHT)
    monkeypatch.setattr(reorder, "filament_reach", lambda session, state, today, spools=None: seen.append(today) or [])
    from conftest import sm_of

    with sm_of(bb_client).begin() as s:  # a known stock: the reach is computed from the record
        make_snapshot(s, date(2026, 11, 20), 1, make_sort(s).id, 800.0)
    fake_bambuddy.unavailable = True
    assert bb_client.get("/").status_code == 200
    assert seen == [date(2026, 11, 21)]


def test_west_of_utc_a_spool_archived_after_the_update_keeps_one_row(db, monkeypatch):
    """Review p719, 1: on a server west of UTC, 1.2.2 has already written rows on the next UTC day. The
    first tick after the update counts the day before; a spool archived then must not get a second row on
    that day (the unique index would roll back every tick until midnight). Its last row stays the later
    one, marked archived; the row of the day before stays too, so its use is kept."""
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(localtime, "SERVER_ZONE", ZoneInfo("America/New_York"))
    spool = FakeBambuddy().add_spool("PETG", brand="Brand A", remaining_g=300.0)
    with db.begin() as s:
        filament.record_snapshot(s, [spool], date(2026, 11, 20))  # 1.2.2 at 13:00 EST, UTC day 20
        filament.record_snapshot(s, [dataclasses.replace(spool, remaining_g=250.0)], date(2026, 11, 21))  # 20:30 EST
    now = datetime(2026, 11, 21, 2, 0)  # 21:00 EST on the 20th, after the update
    archived = dataclasses.replace(spool, remaining_g=200.0, archived=True, archived_at=now)
    with db.begin() as s:
        filament.record_snapshot(s, [archived], filament.record_day(now))
    with db() as s:
        rows = [(r.day, r.remaining_g, r.archived) for r in s.scalars(select(FilamentSnapshot).order_by(FilamentSnapshot.day))]
        assert rows == [(date(2026, 11, 20), 300.0, False), (date(2026, 11, 21), 200.0, True)]
