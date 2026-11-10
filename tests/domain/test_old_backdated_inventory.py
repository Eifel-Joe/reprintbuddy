"""p301: a count dated back before v0.3.16 holds for the end of its day too (Spec 6.2, p260/p261).

Before v0.3.16 a count dated back was stored at the clock time it was typed in on that day (Monday
14:00). Nothing is rewritten: when a booking dated back is checked against later counts, every count
dated back holds until the end of its day, so a sale dated to that Monday and typed in at 16:00 is
refused like with a count of today's kind. A count that was booked on its own day ("today" then)
holds at its moment, as before. Rows dated back are told by the two times of the row: a booking of
"today" has the same time for when it was saved and when it counts.
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from factories import make_article, make_part
from reprintbuddy.domain import stock
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.localtime import at_local_time, local_date
from reprintbuddy.models import Article, ArticlePart, JournalEntry, Part, utcnow


def day(n):
    return local_date(utcnow()) - timedelta(days=n)


def old_count(s, field_target, n_days, clock, counted, *, part=False):
    """A count dated back the way 0.3.14 and 0.3.15 stored it: at the clock time of typing it in."""
    at = at_local_time(day(n_days), clock)
    if part:
        stock.count_part(s, field_target, counted, "user", booked_at=at)
    else:
        stock.count_article(s, field_target, "stock", counted, "user", booked_at=at)
    return at


def counted_that_day(call):
    with pytest.raises(ValidationError) as exc:
        call()
    assert exc.value.key == "bookings.counted_later"
    return exc.value.params


def test_a_sale_later_that_day_is_refused_by_an_old_count(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=10).id
        at = old_count(s, a, 2, time(14, 0), 10)
        later_that_day = at_local_time(day(2), time(16, 30))
        params = counted_that_day(lambda: stock.sell_unreserved(s, a, 1, "user", booked_at=later_that_day))
        assert params["item"] == "Template A"
        assert local_date(params["at"]) == day(2)  # the message names the day of the count
        assert s.get(Article, a).stock == 10
        # the next day lies after the count: booked
        stock.sell_unreserved(s, a, 1, "user", booked_at=at_local_time(day(1), time(9, 0)))
        assert s.get(Article, a).stock == 9
        # nothing is rewritten: the count keeps its time
        assert s.scalars(select(JournalEntry.booked_at).where(JournalEntry.kind == "inventory")).one() == at


def test_a_stock_in_later_that_day_is_refused_by_an_old_count(db):  # sister path
    with db.begin() as s:
        a = make_article(s, stock=4).id
        old_count(s, a, 3, time(8, 0), 4)
        counted_that_day(lambda: stock.stock_in(
            s, a, 2, "user", consume_parts=False, booked_at=at_local_time(day(3), time(20, 0))
        ))
        assert s.get(Article, a).stock == 4


def test_a_part_receipt_later_that_day_is_refused_by_an_old_count(db):  # sister path: purchased parts
    with db.begin() as s:
        p = make_part(s, name="Insert M4", stock=0).id
        old_count(s, p, 2, time(11, 0), 30, part=True)
        params = counted_that_day(lambda: stock.receive_part(
            s, p, 5, "user", booked_at=at_local_time(day(2), time(18, 0))
        ))
        assert params["item"] == "Insert M4"
        assert s.get(Part, p).stock == 30


def test_a_count_booked_on_its_own_day_holds_at_its_moment(db):
    """A count typed in as "today" two days ago is not dated back: a sale dated to that day after it
    is booked (Spec 6.2: today's count holds "now")."""
    with db.begin() as s:
        a = make_article(s, stock=10).id
        stock.count_article(s, a, "stock", 10, "user")
        entry = s.scalars(select(JournalEntry).where(JournalEntry.kind == "inventory")).one()
        entry.at = entry.booked_at = at_local_time(day(2), time(14, 0))
        s.flush()
        stock.sell_unreserved(s, a, 1, "user", booked_at=at_local_time(day(2), time(16, 0)))
        assert s.get(Article, a).stock == 9


def test_a_stock_in_taking_parts_off_is_refused_by_an_old_count_of_a_part(db):  # sister: "Deduct parts"
    with db.begin() as s:
        p = make_part(s, name="Insert M4", stock=20).id
        a = make_article(s, stock=0).id
        s.add(ArticlePart(article_id=a, part_id=p, qty_per_unit=2))
        s.flush()
        old_count(s, p, 2, time(10, 0), 20, part=True)
        that_evening = at_local_time(day(2), time(19, 0))
        params = counted_that_day(lambda: stock.stock_in(s, a, 3, "user", consume_parts=True, booked_at=that_evening))
        assert params["item"] == "Insert M4"
        assert (s.get(Article, a).stock, s.get(Part, p).stock) == (0, 20)
        stock.stock_in(s, a, 3, "user", consume_parts=False, booked_at=that_evening)  # the article was not counted
        assert (s.get(Article, a).stock, s.get(Part, p).stock) == (3, 20)


def test_an_old_count_of_awaiting_assembly_does_not_block_a_sale(db):  # sister: only the value it changes
    with db.begin() as s:
        a = make_article(s, stock=5, awaiting_assembly=2).id
        stock.count_article(
            s, a, "awaiting_assembly", 2, "user", booked_at=at_local_time(day(2), time(9, 0))
        )
        stock.sell_unreserved(s, a, 1, "user", booked_at=at_local_time(day(2), time(15, 0)))
        assert s.get(Article, a).stock == 4


def test_an_old_count_just_after_midnight_holds_until_late_that_day(db):  # review: the lower bound
    with db.begin() as s:
        a = make_article(s, stock=10).id
        old_count(s, a, 3, time(0, 30), 10)
        counted_that_day(lambda: stock.sell_unreserved(s, a, 1, "user", booked_at=at_local_time(day(3), time(23, 30))))


def test_a_count_of_0_3_16_stays_at_its_end_of_day_when_the_zone_changes(db, monkeypatch):  # review finding 1
    """A count dated back with 0.3.16 is stored at the end of its day in the zone of then. After a change
    of the zone (UTC to Berlin) it falls on 00:59 of the next day there; it must not hold until the end
    of that next day."""
    from reprintbuddy import localtime

    berlin = localtime.LOCAL_ZONE
    with db.begin() as s:
        a = make_article(s, stock=10).id
        monkeypatch.setattr(localtime, "LOCAL_ZONE", UTC)
        counted_on = day(3)
        stock.count_article(s, a, "stock", 10, "user", booked_at=localtime.end_of_local_day(counted_on))
        monkeypatch.setattr(localtime, "LOCAL_ZONE", berlin)
        next_morning = at_local_time(counted_on + timedelta(days=1), time(9, 0))
        stock.sell_unreserved(s, a, 1, "user", booked_at=next_morning)
        assert s.get(Article, a).stock == 9


def test_an_old_count_typed_late_in_winter_for_a_summer_day_keeps_its_day(db):  # review finding 2
    """0.3.14 and 0.3.15 put the clock time of typing on the chosen day with the offset of the day of
    typing. Typed on 5 November at 23:30 (UTC+1) for 1 July (UTC+2) it was stored at 22:30 UTC, which
    is 2 July 00:30 in summer time. It holds until the end of 1 July, the day that was picked."""
    with db.begin() as s:
        a = make_article(s, stock=10).id
        stock.count_article(s, a, "stock", 10, "user", booked_at=datetime(2026, 7, 1, 22, 30))
        entry = s.scalars(select(JournalEntry).where(JournalEntry.kind == "inventory")).one()
        entry.at = datetime(2026, 11, 5, 22, 30)
        s.flush()
        params = counted_that_day(lambda: stock.sell_unreserved(
            s, a, 1, "user", booked_at=at_local_time(date(2026, 7, 1), time(23, 45))
        ))
        assert local_date(params["at"]) == date(2026, 7, 1)  # the message names the day that was picked
        stock.sell_unreserved(s, a, 1, "user", booked_at=at_local_time(date(2026, 7, 2), time(9, 0)))
        assert s.get(Article, a).stock == 9
