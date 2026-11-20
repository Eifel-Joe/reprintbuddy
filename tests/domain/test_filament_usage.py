"""The views of the column "Verbrauch" on the filament page (Spec 9.3, p717).

Ø 30 days (the default and the base of the reorder suggestions), Ø 7 days, today, yesterday, this week
(ISO calendar week from Monday), this month, this year (calendar periods) and total (since the record
began). Days are the days of the record (UTC, Spec 10.5a). The rule is that of `consumption` (Spec 9.2):
only the decreases between two consecutive rows of a spool count. A drop belongs to the day of its later
row: a period from day S counts the drops that arrive on S up to today, from the row of the day before S
on (as "today" counts the drop from yesterday's row). An average divides by the days between the first
and the last row of the window, at most the window.
"""

import random
from datetime import date, timedelta

import pytest

from factories import make_snapshot, make_sort
from reprintbuddy.domain import filament

TODAY = date(2026, 11, 20)  # a Friday in ISO week 47


def _views(s, sort_ids, today=TODAY):
    return filament.usage(s, sort_ids, today)


def _daily(s, sort, today, drops, start_back=400, spool=1):
    """Rows of one spool from `start_back` days before `today` to today; `drops`: day -> grams used that day."""
    grams = 100_000.0
    for back in range(start_back, -1, -1):
        day = today - timedelta(days=back)
        grams -= drops.get(day, 0)
        make_snapshot(s, day, spool, sort, grams)


def test_the_views_and_their_order():
    assert filament.USAGE_VIEWS == ("avg30", "avg7", "today", "yesterday", "week", "month", "year", "total")
    assert filament.USAGE_DEFAULT == "avg30"
    assert filament.USAGE_WINDOW == {"avg30": 30, "avg7": 7}  # the averages and their days


@pytest.mark.parametrize(("today", "week", "month", "year"), [
    (date(2026, 11, 20), date(2026, 11, 16), date(2026, 11, 1), date(2026, 1, 1)),
    (date(2026, 11, 16), date(2026, 11, 16), date(2026, 11, 1), date(2026, 1, 1)),  # a Monday
    (date(2026, 11, 1), date(2026, 10, 26), date(2026, 11, 1), date(2026, 1, 1)),  # the 1st, a Sunday
    (date(2027, 1, 1), date(2026, 12, 28), date(2027, 1, 1), date(2027, 1, 1)),  # the week began last year
    (date(2026, 12, 31), date(2026, 12, 28), date(2026, 12, 1), date(2026, 1, 1)),
])
def test_the_first_day_of_each_calendar_period(today, week, month, year):
    assert filament.usage_starts(today) == {"week": week, "month": month, "year": year}


def test_every_view_of_a_steady_use(db):
    """10 g a day for 400 days, on Friday 20 November 2026: week from Monday 16th (5 days), month from the
    1st (20 days), year from 1 January (324 days)."""
    with db.begin() as s:
        sort = make_sort(s).id
        for back in range(401):
            make_snapshot(s, TODAY - timedelta(days=back), 1, sort, 5000.0 - 10.0 * (400 - back))
    with db.begin() as s:
        got = _views(s, [sort])
    grams = {view: round(got[view][sort].grams, 6) for view in filament.USAGE_VIEWS}
    assert grams == {
        "avg30": 10.0, "avg7": 10.0, "today": 10.0, "yesterday": 10.0,
        "week": 50.0, "month": 200.0, "year": 3240.0, "total": 4000.0,
    }
    assert (got["avg30"][sort].days, got["avg7"][sort].days) == (30, 7)


@pytest.mark.parametrize(("today", "inside", "outside", "view"), [
    (date(2026, 11, 20), date(2026, 11, 16), date(2026, 11, 15), "week"),  # Monday counts, Sunday not
    (date(2027, 1, 1), date(2026, 12, 28), date(2026, 12, 27), "week"),  # across the turn of the year
    (date(2026, 11, 20), date(2026, 11, 1), date(2026, 10, 31), "month"),
    (date(2026, 3, 2), date(2026, 3, 1), date(2026, 2, 28), "month"),
    (date(2026, 11, 20), date(2026, 1, 1), date(2025, 12, 31), "year"),
    (date(2027, 1, 1), date(2027, 1, 1), date(2026, 12, 31), "year"),  # on 1 January: only that day
])
def test_the_edges_of_the_calendar_periods(db, today, inside, outside, view):
    """1 g on the first day of the period, 100 g on the day before: only the 1 g is in it."""
    with db.begin() as s:
        sort = make_sort(s).id
        _daily(s, sort, today, {inside: 1.0, outside: 100.0})
    with db.begin() as s:
        assert _views(s, [sort], today)[view][sort].grams == 1.0


def test_the_edges_of_the_other_views(db):
    """1 g today, 2 g yesterday, 4 g the day before, 8 g on day −6, 16 g on day −7, 32 g on day −29,
    64 g on day −30. A drop on day d is the decrease from the row of day d−1 to that of day d."""
    drops = {TODAY - timedelta(days=back): grams for back, grams in {0: 1, 1: 2, 2: 4, 6: 8, 7: 16, 29: 32, 30: 64}.items()}
    with db.begin() as s:
        sort = make_sort(s).id
        _daily(s, sort, TODAY, drops)
    with db.begin() as s:
        got = _views(s, [sort])
    assert (got["today"][sort].grams, got["yesterday"][sort].grams, got["total"][sort].grams) == (1, 2, 127)
    assert got["avg7"][sort].grams == pytest.approx(15 / 7)
    assert got["avg30"][sort].grams == pytest.approx(63 / 30)


def test_the_first_day_of_the_record(db):
    with db.begin() as s:
        assert filament.first_record_day(s) is None
        sort = make_sort(s).id
        make_snapshot(s, date(2026, 11, 14), 1, sort, 500.0)
        make_snapshot(s, date(2026, 11, 19), 2, sort, 500.0)
        assert filament.first_record_day(s) == date(2026, 11, 14)


def test_averages_say_over_how_many_days_while_the_record_is_short(db):
    """Six days of record: Ø 30 and Ø 7 divide by 6 ("bisher 6"), not by the window."""
    with db.begin() as s:
        sort = make_sort(s).id
        for back in range(7):
            make_snapshot(s, TODAY - timedelta(days=back), 1, sort, 600.0 - 12.0 * (6 - back))
    with db.begin() as s:
        got = _views(s, [sort])
    assert (got["avg30"][sort].grams, got["avg30"][sort].days) == (12.0, 6)
    assert (got["avg7"][sort].grams, got["avg7"][sort].days) == (12.0, 6)
    assert got["total"][sort].grams == 72.0


def test_without_two_days_of_record_a_view_has_nothing(db):
    """A sort recorded only today has no use yet in any view; yesterday needs rows of yesterday and the day before."""
    with db.begin() as s:
        new = make_sort(s).id
        make_snapshot(s, TODAY, 1, new, 1000.0)
        recent = make_sort(s, brand="Brand B").id
        make_snapshot(s, TODAY - timedelta(days=1), 2, recent, 1000.0)
        make_snapshot(s, TODAY, 2, recent, 900.0)
    with db.begin() as s:
        got = _views(s, [new, recent])
    assert all(new not in got[view] for view in filament.USAGE_VIEWS)
    assert recent not in got["yesterday"]
    assert got["today"][recent].grams == 100.0


def test_a_day_without_a_drop_shows_zero(db):
    with db.begin() as s:
        sort = make_sort(s).id
        for back in range(3):
            make_snapshot(s, TODAY - timedelta(days=back), 1, sort, 500.0)
    with db.begin() as s:
        got = _views(s, [sort])
    assert (got["today"][sort].grams, got["avg30"][sort].grams) == (0.0, 0.0)


def test_sorts_are_kept_apart_and_an_increase_counts_nothing(db):
    with db.begin() as s:
        a = make_sort(s).id
        b = make_sort(s, brand="Brand B").id
        make_snapshot(s, TODAY - timedelta(days=1), 1, a, 500.0)
        make_snapshot(s, TODAY, 1, a, 450.0)
        make_snapshot(s, TODAY - timedelta(days=1), 2, b, 100.0)
        make_snapshot(s, TODAY, 2, b, 1000.0)  # a new roll weighed into the same spool: no use
    with db.begin() as s:
        got = _views(s, [a, b])
    assert (got["today"][a].grams, got["today"][b].grams) == (50.0, 0.0)
    assert _views(s, []) == {view: {} for view in filament.USAGE_VIEWS}


def test_the_last_row_of_an_archived_spool_counts_for_the_use(db):
    with db.begin() as s:
        sort = make_sort(s).id
        make_snapshot(s, TODAY - timedelta(days=1), 1, sort, 252.75)
        make_snapshot(s, TODAY, 1, sort, 83.59, archived=True)
    with db.begin() as s:
        assert round(_views(s, [sort])["today"][sort].grams, 2) == 169.16


def test_rows_of_other_sorts_and_after_today_do_not_count(db):
    with db.begin() as s:
        sort = make_sort(s).id
        other = make_sort(s, brand="Brand B").id
        make_snapshot(s, TODAY - timedelta(days=1), 1, sort, 500.0)
        make_snapshot(s, TODAY, 1, sort, 400.0)
        make_snapshot(s, TODAY + timedelta(days=1), 1, sort, 100.0)
        make_snapshot(s, TODAY - timedelta(days=1), 2, other, 900.0)
        make_snapshot(s, TODAY, 2, other, 100.0)
    with db.begin() as s:
        got = _views(s, [sort])
    assert set(got["total"]) == {sort}
    assert got["total"][sort].grams == 100.0


@pytest.mark.parametrize("seed", range(5))
def test_the_page_average_of_30_days_is_the_reorder_one(db, seed):
    """Ø 30 days is the base of the reorder suggestions (Spec 9.3): the page shows the same number as
    `consumption`, with gaps, refills, new and archived spools."""
    rng = random.Random(seed)
    with db.begin() as s:
        sorts = [make_sort(s, brand=f"Brand {n}").id for n in range(3)]
        spool = 0
        for sort in sorts:
            for _ in range(rng.randint(1, 4)):
                spool += 1
                grams = rng.uniform(200, 1000)
                start = rng.randint(0, 45)
                for back in range(start, -1, -1):
                    if rng.random() < 0.2:
                        continue  # a day without a record
                    grams = max(0.0, grams - rng.choice([0, 0, 5, 30, 120])) if rng.random() > 0.05 else 1000.0
                    make_snapshot(s, TODAY - timedelta(days=back), spool, sort, grams)
    with db.begin() as s:
        got = _views(s, sorts)
        for sort in sorts:
            per_day, days = filament.consumption(s, sort, TODAY)
            if days:
                assert got["avg30"][sort].grams == pytest.approx(per_day)
                assert got["avg30"][sort].days == days
            else:
                assert sort not in got["avg30"]


def test_a_sort_with_rows_older_than_a_year_only_has_only_its_total(db):
    """Review p717, 1: the query of the last year has no group for such a sort; the page must not fail."""
    with db.begin() as s:
        old = make_sort(s).id
        recent = make_sort(s, brand="Brand B").id
        make_snapshot(s, TODAY - timedelta(days=400), 1, old, 500.0)
        make_snapshot(s, TODAY - timedelta(days=399), 1, old, 450.0)
        make_snapshot(s, TODAY - timedelta(days=1), 2, recent, 500.0)
        make_snapshot(s, TODAY, 2, recent, 480.0)
    with db.begin() as s:
        got = _views(s, [old, recent])
        assert filament.consumption(s, old, TODAY) == (0.0, 0)
    assert [view for view in filament.USAGE_VIEWS if old in got[view]] == ["total"]
    assert got["total"][old].grams == 50.0
    assert got["today"][recent].grams == 20.0
