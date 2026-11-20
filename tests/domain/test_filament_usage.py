"""The views of the column "Verbrauch" on the filament page (Spec 9.3, p717).

Ø 30 days (the default and the base of the reorder suggestions), Ø 7 days, today, yesterday, week (last 7
days), month (last 30 days), year (last 365 days) and total (since the record began). Days are the days of
the record (UTC, Spec 10.5a). The rule is that of `consumption` (Spec 9.2): only the decreases between two
consecutive rows of a spool count, both inside the window; an average divides by the days between the
first and the last row of the window, at most the window.
"""

import random
from datetime import date, timedelta

import pytest

from factories import make_snapshot, make_sort
from reprintbuddy.domain import filament

TODAY = date(2026, 11, 20)


def _views(s, sort_ids, today=TODAY):
    return filament.usage(s, sort_ids, today)


def test_the_views_and_their_order():
    assert filament.USAGE_VIEWS == ("avg30", "avg7", "today", "yesterday", "week", "month", "year", "total")
    assert filament.USAGE_DEFAULT == "avg30"
    assert {view: filament.USAGE_WINDOW[view] for view in filament.USAGE_VIEWS} == {
        "avg30": 30, "avg7": 7, "today": 1, "yesterday": 1, "week": 7, "month": 30, "year": 365, "total": None,
    }


def test_every_view_of_a_steady_use(db):
    """10 g a day for 400 days: the sums cover their days, the averages give 10 g."""
    with db.begin() as s:
        sort = make_sort(s).id
        for back in range(401):
            make_snapshot(s, TODAY - timedelta(days=back), 1, sort, 5000.0 - 10.0 * (400 - back))
    with db.begin() as s:
        got = _views(s, [sort])
    assert {view: (round(got[view][sort].grams, 6), got[view][sort].days) for view in filament.USAGE_VIEWS} == {
        "avg30": (10.0, 30), "avg7": (10.0, 7), "today": (10.0, 1), "yesterday": (10.0, 1),
        "week": (70.0, 7), "month": (300.0, 30), "year": (3650.0, 365), "total": (4000.0, 400),
    }


def test_the_edges_of_the_windows(db):
    """One drop on each of the days around the edges: 1 g today, 2 g yesterday, 4 g the day before,
    8 g on day −6, 16 g on day −7, 32 g on day −29, 64 g on day −30, 128 g on day −364, 256 g on day −365.
    A drop on day d is the decrease from the row of day d−1 to that of day d."""
    drops = {0: 1, 1: 2, 2: 4, 6: 8, 7: 16, 29: 32, 30: 64, 364: 128, 365: 256}
    with db.begin() as s:
        sort = make_sort(s).id
        grams = 10_000.0
        for back in range(400, -1, -1):
            grams -= drops.get(back, 0)
            make_snapshot(s, TODAY - timedelta(days=back), 1, sort, grams)
    with db.begin() as s:
        got = _views(s, [sort])
    sums = {view: got[view][sort].grams for view in ("today", "yesterday", "week", "month", "year", "total")}
    assert sums == {
        "today": 1, "yesterday": 2, "week": 1 + 2 + 4 + 8, "month": 1 + 2 + 4 + 8 + 16 + 32,
        "year": 1 + 2 + 4 + 8 + 16 + 32 + 64 + 128, "total": 511,
    }
    assert got["avg7"][sort].grams == pytest.approx(15 / 7)
    assert got["avg30"][sort].grams == pytest.approx(63 / 30)


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
    assert (got["total"][sort].grams, got["month"][sort].grams) == (72.0, 72.0)


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
