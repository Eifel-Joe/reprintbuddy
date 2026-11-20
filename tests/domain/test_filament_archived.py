"""An archived spool keeps its use up to its last weight, also on the day it is archived (Spec 9.2, p717).

Until 1.2.1 the daily record dropped the row of a spool archived on the same day: a spool printed empty
and archived that day lost its use of the day. Now the spool keeps one row marked `archived` with its
last weight, on the day Bambuddy archived it (never before its last row, never after today). It counts
for the use, not for the stock or the number of spools.
"""

import dataclasses
from datetime import date, datetime, timedelta

from sqlalchemy import select

from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.domain import filament, reorder
from reprintbuddy.models import FilamentSnapshot

DAY = date(2026, 3, 10)


def _spools(*grams: float):
    fake = FakeBambuddy()
    return [fake.add_spool("PETG", brand="Brand A", remaining_g=g) for g in grams]


def _archived(spool, grams: float, at: datetime | None = None):
    return dataclasses.replace(spool, remaining_g=grams, archived=True, archived_at=at)


def _rows(s):
    return [
        (r.day, r.spool_id, r.remaining_g, r.archived)
        for r in s.scalars(select(FilamentSnapshot).order_by(FilamentSnapshot.day, FilamentSnapshot.spool_id))
    ]


def test_a_spool_printed_empty_and_archived_the_same_day_keeps_its_use(db):
    empty, full = _spools(252.75, 1000.0)
    with db.begin() as s:
        filament.record_snapshot(s, [empty, full], DAY)
        # earlier today: printing has begun
        filament.record_snapshot(s, [dataclasses.replace(empty, remaining_g=150.0), full], DAY + timedelta(days=1))
        # later today: printed down to its last grams and archived, the next spool took over
        now = datetime(2026, 3, 11, 16, 30)
        filament.record_snapshot(
            s, [_archived(empty, 83.59, now), dataclasses.replace(full, remaining_g=720.19)], DAY + timedelta(days=1)
        )
    with db.begin() as s:
        sort = s.scalar(select(FilamentSnapshot.spool_filament_id))
        per_day, days = filament.consumption(s, sort, DAY + timedelta(days=1))
        assert days == 1
        assert round(per_day, 2) == round((252.75 - 83.59) + (1000.0 - 720.19), 2)
        stock, latest = filament.stock_from_snapshots(s)
        (entry,) = stock.values()
        assert latest == DAY + timedelta(days=1)
        assert (round(entry.stock_g, 2), entry.spools) == (720.19, 1)


def test_archived_without_a_record_earlier_that_day_counts_too(db):
    (spool,) = _spools(300.0)
    with db.begin() as s:
        filament.record_snapshot(s, [spool], DAY)
        filament.record_snapshot(s, [_archived(spool, 0.0, datetime(2026, 3, 11, 9, 0))], DAY + timedelta(days=1))
    with db.begin() as s:
        assert _rows(s) == [(DAY, spool.id, 300.0, False), (DAY + timedelta(days=1), spool.id, 0.0, True)]
        sort = s.scalar(select(FilamentSnapshot.spool_filament_id))
        assert filament.consumption(s, sort, DAY + timedelta(days=1)) == (300.0, 1)
        stock, latest = filament.stock_from_snapshots(s)
        assert latest == DAY + timedelta(days=1)
        assert stock == {}


def test_a_spool_archived_with_its_rest_uses_nothing_and_leaves_the_stock(db):
    keep, other = _spools(400.0, 900.0)
    with db.begin() as s:
        filament.record_snapshot(s, [keep, other], DAY)
        filament.record_snapshot(s, [_archived(keep, 400.0, datetime(2026, 3, 11, 8, 0)), other], DAY + timedelta(days=1))
    with db.begin() as s:
        sort = s.scalar(select(FilamentSnapshot.spool_filament_id))
        assert filament.consumption(s, sort, DAY + timedelta(days=1)) == (0.0, 1)
        stock, _ = filament.stock_from_snapshots(s)
        (entry,) = stock.values()
        assert (entry.stock_g, entry.spools) == (900.0, 1)


def test_an_archived_spool_gets_one_row_and_no_more(db):
    (spool,) = _spools(300.0)
    archived = _archived(spool, 0.0, datetime(2026, 3, 11, 9, 0))
    with db.begin() as s:
        filament.record_snapshot(s, [spool], DAY)
        for back in (1, 2, 3):
            filament.record_snapshot(s, [archived], DAY + timedelta(days=back))
        filament.record_snapshot(s, [archived], DAY + timedelta(days=3))
    with db.begin() as s:
        assert _rows(s) == [(DAY, spool.id, 300.0, False), (DAY + timedelta(days=1), spool.id, 0.0, True)]


def test_the_row_lands_on_the_day_bambuddy_archived_the_spool(db):
    """A pause of the record (or the update from 1.2.1, which had dropped that row) does not move the
    use to today: the row lands on the day of `archived_at`."""
    (spool,) = _spools(500.0)
    with db.begin() as s:
        filament.record_snapshot(s, [spool], DAY)
        filament.record_snapshot(s, [_archived(spool, 20.0, datetime(2026, 3, 13, 23, 50))], DAY + timedelta(days=10))
    with db.begin() as s:
        assert _rows(s) == [(DAY, spool.id, 500.0, False), (date(2026, 3, 13), spool.id, 20.0, True)]


def test_the_row_is_never_before_the_last_row_nor_after_today(db):
    early, late, unknown = _spools(500.0, 600.0, 700.0)
    with db.begin() as s:
        filament.record_snapshot(s, [early, late, unknown], DAY)
        filament.record_snapshot(s, [early, late, unknown], DAY + timedelta(days=2))
        filament.record_snapshot(
            s,
            [
                _archived(early, 450.0, datetime(2026, 3, 1, 12, 0)),  # before its last row: that row
                _archived(late, 550.0, datetime(2026, 4, 1, 12, 0)),  # after today: today
                _archived(unknown, 650.0, None),  # no time: today
            ],
            DAY + timedelta(days=3),
        )
    with db.begin() as s:
        rows = [r for r in _rows(s) if r[0] > DAY]
        assert rows == [
            (DAY + timedelta(days=2), early.id, 450.0, True),
            (DAY + timedelta(days=2), late.id, 600.0, False),
            (DAY + timedelta(days=2), unknown.id, 700.0, False),
            (DAY + timedelta(days=3), late.id, 550.0, True),
            (DAY + timedelta(days=3), unknown.id, 650.0, True),
        ]


def test_a_spool_archived_before_it_was_ever_recorded_gets_no_row(db):
    (spool,) = _spools(300.0)
    with db.begin() as s:
        filament.record_snapshot(s, [_archived(spool, 0.0, datetime(2026, 3, 10, 9, 0))], DAY)
        assert _rows(s) == []


def test_a_spool_taken_out_of_the_archive_is_recorded_again_and_archived_anew(db):
    (spool,) = _spools(300.0)
    with db.begin() as s:
        filament.record_snapshot(s, [spool], DAY)
        filament.record_snapshot(s, [_archived(spool, 300.0, datetime(2026, 3, 11, 9, 0))], DAY + timedelta(days=1))
        filament.record_snapshot(s, [dataclasses.replace(spool, remaining_g=300.0)], DAY + timedelta(days=2))
        filament.record_snapshot(s, [_archived(spool, 100.0, datetime(2026, 3, 13, 9, 0))], DAY + timedelta(days=3))
    with db.begin() as s:
        assert [(r[0] - DAY).days for r in _rows(s)] == [0, 1, 2, 3]
        assert [r[3] for r in _rows(s)] == [False, True, False, True]


def test_a_deleted_spool_still_leaves_the_day(db):
    """Only archiving is covered by Spec 9.2; a spool gone from the list loses its row of the day as before."""
    keep, gone = _spools(400.0, 300.0)
    with db.begin() as s:
        filament.record_snapshot(s, [keep, gone], DAY)
        filament.record_snapshot(s, [keep], DAY)
        assert _rows(s) == [(DAY, keep.id, 400.0, False)]


def test_arrived_spools_from_the_record_leave_out_archived_rows(db):
    """Without Bambuddy the arrival of ordered spools reads the latest record (Spec 9.4): an archived
    spool is no new spool."""
    keep, gone = _spools(1000.0, 50.0)
    with db.begin() as s:
        filament.record_snapshot(s, [keep, gone], DAY)
        filament.record_snapshot(s, [keep, _archived(gone, 0.0, datetime(2026, 3, 10, 18, 0))], DAY)
    with db.begin() as s:
        seen = reorder._spools_seen(s, None)
        assert [spool.label_g for spool in seen] == [1000]
