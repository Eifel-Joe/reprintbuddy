"""p210: an inventory dated back counts the difference against the stock of that time (Spec 6.2).

Example of the spec proposal: counted 10 yesterday, 2 sold this morning (stock now 8). The
inventory with yesterday's date finds 12 then, books -2 and leaves 8 now; the sale stays.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from factories import make_article, make_part
from reprintbuddy.domain import stock
from reprintbuddy.domain.errors import StockLimitError, ValidationError
from reprintbuddy.models import Article, JournalEntry, Part, utcnow


def last_inventory(s):
    return s.scalars(
        select(JournalEntry).where(JournalEntry.kind == "inventory").order_by(JournalEntry.id.desc())
    ).first()


def test_inventory_dated_back_keeps_the_later_sale(db):
    yesterday = utcnow() - timedelta(days=1)
    with db.begin() as s:
        a = make_article(s, stock=0).id
        stock.stock_in(s, a, 12, "user", consume_parts=False, booked_at=yesterday - timedelta(hours=1))
        stock.sell_unreserved(s, a, 2, "user")  # this morning: stock 10
        assert stock.count_article(s, a, "stock", 10, "user", booked_at=yesterday) == -2
        assert s.get(Article, a).stock == 8  # the sale of today stays
        row = last_inventory(s)
        assert row.qty == -2 and row.booked_at == yesterday
        assert row.details["counted"] == 10
        assert row.details["then"] == [12, 10]  # the stock of that time, before and after
        assert row.details["changes"] == {"stock": [10, 8]}  # the counter now


def test_inventory_dated_back_without_later_bookings_is_the_plain_count(db):
    with db.begin() as s:
        a = make_article(s, stock=5).id
        assert stock.count_article(s, a, "stock", 3, "user", booked_at=utcnow() - timedelta(days=2)) == -2
        assert s.get(Article, a).stock == 3
        assert last_inventory(s).details["then"] == [5, 3]


def test_inventory_of_today_has_no_value_of_that_time(db):
    with db.begin() as s:
        a = make_article(s, stock=5).id
        stock.count_article(s, a, "stock", 3, "user")
        assert "then" not in last_inventory(s).details


def test_bookings_before_the_date_count_as_before(db):
    with db.begin() as s:
        a = make_article(s, stock=0).id
        stock.stock_in(s, a, 5, "user", consume_parts=False, booked_at=utcnow() - timedelta(days=3))
        stock.sell_unreserved(s, a, 1, "user", booked_at=utcnow() - timedelta(days=2))
        # counted 3 a day ago: the stock then was 4, both bookings lie before
        assert stock.count_article(s, a, "stock", 3, "user", booked_at=utcnow() - timedelta(days=1)) == -1
        assert s.get(Article, a).stock == 3


def test_only_the_counted_field_is_replayed(db):
    yesterday = utcnow() - timedelta(days=1)
    with db.begin() as s:
        a = make_article(s, stock=4, awaiting_assembly=3).id
        stock.assemble(s, a, 2, "user")  # today: awaiting 3 -> 1, stock 4 -> 6
        assert stock.count_article(s, a, "awaiting_assembly", 2, "user", booked_at=yesterday) == -1
        art = s.get(Article, a)
        assert (art.awaiting_assembly, art.stock) == (0, 6)
        assert last_inventory(s).details["then"] == [3, 2]


def test_inventory_dated_back_is_rejected_when_a_later_inventory_exists(db):
    with db.begin() as s:
        a = make_article(s, stock=5).id
        stock.count_article(s, a, "stock", 7, "user", booked_at=utcnow() - timedelta(days=1))
        before = s.get(Article, a).stock
        with pytest.raises(ValidationError) as exc:
            stock.count_article(s, a, "stock", 3, "user", booked_at=utcnow() - timedelta(days=2))
        assert exc.value.key == "bookings.later_inventory"
        assert s.get(Article, a).stock == before


def test_a_later_inventory_of_another_field_does_not_block(db):
    with db.begin() as s:
        a = make_article(s, stock=5, awaiting_assembly=2).id
        stock.count_article(s, a, "awaiting_assembly", 1, "user")
        stock.count_article(s, a, "stock", 4, "user", booked_at=utcnow() - timedelta(days=1))
        assert s.get(Article, a).stock == 4


def test_inventory_dated_back_cannot_make_the_value_negative(db):  # Spec 6.4
    yesterday = utcnow() - timedelta(days=1)
    with db.begin() as s:
        a = make_article(s, stock=0).id
        stock.stock_in(s, a, 5, "user", consume_parts=False, booked_at=yesterday - timedelta(hours=1))
        stock.sell_unreserved(s, a, 4, "user")  # now 1
        with pytest.raises(StockLimitError):
            stock.count_article(s, a, "stock", 2, "user", booked_at=yesterday)  # 1 + (2 - 5) < 0
        assert s.get(Article, a).stock == 1


def test_part_inventory_dated_back_keeps_the_later_use(db):  # sister path: count_part
    yesterday = utcnow() - timedelta(days=1)
    with db.begin() as s:
        p = make_part(s, stock=0).id
        stock.receive_part(s, p, 50, "user", booked_at=yesterday - timedelta(hours=1))
        stock.receive_part(s, p, 10, "user")  # today: 60
        assert stock.count_part(s, p, 48, "user", booked_at=yesterday) == -2
        assert s.get(Part, p).stock == 58
        assert last_inventory(s).details["then"] == [50, 48]


def test_part_inventory_dated_back_is_rejected_when_a_later_inventory_exists(db):
    with db.begin() as s:
        p = make_part(s, stock=5).id
        stock.count_part(s, p, 7, "user")
        with pytest.raises(ValidationError) as exc:
            stock.count_part(s, p, 3, "user", booked_at=utcnow() - timedelta(days=1))
        assert exc.value.key == "bookings.later_inventory"
        assert s.get(Part, p).stock == 7


# ---- review: later bookings are replayed forward from the count -------------------------------


def _with_article_using(s, part_id, per_unit=1):
    from reprintbuddy.models import ArticlePart

    a = make_article(s, stock=0)
    s.add(ArticlePart(article_id=a.id, part_id=part_id, qty_per_unit=per_unit))
    s.flush()
    return a.id


def test_a_later_use_capped_at_zero_takes_its_full_amount_after_a_higher_count(db):  # Spec 8.3
    yesterday = utcnow() - timedelta(days=1)
    with db.begin() as s:
        p = make_part(s, stock=0).id
        a = _with_article_using(s, p)
        stock.stock_in(s, a, 5, "user", consume_parts=True)  # needs 5, had 0: capped at 0
        assert stock.count_part(s, p, 10, "user", booked_at=yesterday) == 10
        assert s.get(Part, p).stock == 5  # 10 then, the 5 used today take it to 5


def test_a_later_use_is_capped_at_zero_after_a_lower_count(db):  # Spec 8.3
    yesterday = utcnow() - timedelta(days=1)
    with db.begin() as s:
        p = make_part(s, stock=0).id
        stock.receive_part(s, p, 10, "user", booked_at=yesterday - timedelta(hours=1))
        a = _with_article_using(s, p)
        stock.stock_in(s, a, 8, "user", consume_parts=True)  # today: 10 -> 2
        assert stock.count_part(s, p, 3, "user", booked_at=yesterday) == -7
        assert s.get(Part, p).stock == 0  # 3 then; the use of 8 is capped at 0, not refused


def test_a_count_that_makes_a_later_booking_go_below_zero_is_refused(db):  # Spec 6.4
    yesterday = utcnow() - timedelta(days=1)
    with db.begin() as s:
        a = make_article(s, stock=5).id
        stock.sell_unreserved(s, a, 3, "user")
        stock.stock_in(s, a, 3, "user", consume_parts=False)  # today 5 again
        with pytest.raises(StockLimitError):
            stock.count_article(s, a, "stock", 0, "user", booked_at=yesterday)  # the sale of 3 from 0
        assert s.get(Article, a).stock == 5
