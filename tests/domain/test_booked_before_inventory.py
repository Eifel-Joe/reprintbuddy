"""p261: a booking dated before an inventory of a value it changes is refused (Spec 6.2).

Example of the spec proposal: counted 10 yesterday; a sale of 2 forgotten three days ago, booked
with that date, made the stock 8 - but the count already holds it. Now it is refused, naming the
item and the day of the count; nothing is booked.
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from factories import make_article, make_part
from reprintbuddy.domain import stock
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.models import Article, ArticlePart, JournalEntry, Part, utcnow


def rows(s):
    return s.scalar(select(func.count()).select_from(JournalEntry))


def days_ago(n):
    return utcnow() - timedelta(days=n)


def refused(call, s):
    before = rows(s)
    with pytest.raises(ValidationError) as exc:
        call()
    assert exc.value.key == "bookings.counted_later"
    assert rows(s) == before  # nothing booked, not even a part of it
    return exc.value.params


def test_a_sale_dated_before_a_count_is_refused(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=12).id
        stock.count_article(s, a, "stock", 10, "user", booked_at=days_ago(1))
        params = refused(lambda: stock.sell_unreserved(s, a, 2, "user", booked_at=days_ago(3)), s)
        assert params["item"] == "Template A"
        assert params["at"] == s.scalars(select(JournalEntry.booked_at).where(JournalEntry.kind == "inventory")).one()
        assert s.get(Article, a).stock == 10


def test_a_stock_in_dated_before_a_count_is_refused(db):  # sister path
    with db.begin() as s:
        a = make_article(s, stock=3).id
        stock.count_article(s, a, "stock", 3, "user")
        refused(lambda: stock.stock_in(s, a, 1, "user", consume_parts=False, booked_at=days_ago(2)), s)
        assert s.get(Article, a).stock == 3


def test_a_part_receipt_dated_before_a_count_is_refused(db):  # sister path: purchased parts
    with db.begin() as s:
        p = make_part(s, name="Insert M4", stock=0).id
        stock.count_part(s, p, 50, "user", booked_at=days_ago(1))
        params = refused(lambda: stock.receive_part(s, p, 20, "user", booked_at=days_ago(2)), s)
        assert params["item"] == "Insert M4"
        assert s.get(Part, p).stock == 50


def test_a_stock_in_using_parts_is_refused_when_a_part_was_counted_later(db):
    with db.begin() as s:
        p = make_part(s, name="Insert M4", stock=20).id
        a = make_article(s, stock=0).id
        s.add(ArticlePart(article_id=a, part_id=p, qty_per_unit=2))
        s.flush()
        stock.count_part(s, p, 20, "user", booked_at=days_ago(1))
        params = refused(lambda: stock.stock_in(s, a, 3, "user", consume_parts=True, booked_at=days_ago(2)), s)
        assert params["item"] == "Insert M4"
        assert (s.get(Article, a).stock, s.get(Part, p).stock) == (0, 20)
        # without taking the parts off, the article was not counted: it is booked
        stock.stock_in(s, a, 3, "user", consume_parts=False, booked_at=days_ago(2))
        assert (s.get(Article, a).stock, s.get(Part, p).stock) == (3, 20)


def test_a_count_of_awaiting_assembly_does_not_block_a_sale(db):  # only the value the booking changes
    with db.begin() as s:
        a = make_article(s, stock=5, awaiting_assembly=2).id
        stock.count_article(s, a, "awaiting_assembly", 1, "user")
        stock.sell_unreserved(s, a, 1, "user", booked_at=days_ago(2))
        assert s.get(Article, a).stock == 4


def test_a_count_of_another_article_or_part_does_not_block(db):
    with db.begin() as s:
        a, other = make_article(s, stock=5).id, make_article(s, stock=5).id
        p, other_part = make_part(s, stock=5).id, make_part(s, stock=5).id
        stock.count_article(s, other, "stock", 5, "user")
        stock.count_part(s, other_part, 5, "user")
        stock.sell_unreserved(s, a, 1, "user", booked_at=days_ago(2))
        stock.receive_part(s, p, 1, "user", booked_at=days_ago(2))
        assert (s.get(Article, a).stock, s.get(Part, p).stock) == (4, 6)


def test_bookings_after_the_count_work_as_before(db):
    with db.begin() as s:
        a = make_article(s, stock=10).id
        stock.count_article(s, a, "stock", 10, "user", booked_at=days_ago(3))
        stock.sell_unreserved(s, a, 1, "user", booked_at=days_ago(2))  # dated after the count
        stock.sell_unreserved(s, a, 1, "user")  # today
        assert s.get(Article, a).stock == 8


def test_a_booking_of_today_after_a_count_of_today_works(db):
    with db.begin() as s:
        a = make_article(s, stock=10).id
        stock.count_article(s, a, "stock", 10, "user")
        stock.sell_unreserved(s, a, 1, "user")
        assert s.get(Article, a).stock == 9


def test_a_second_count_dated_to_the_same_day_replaces_the_first(db):  # review: p260 + p261
    with db.begin() as s:
        a = make_article(s, stock=10).id
        day_end = days_ago(1)
        stock.count_article(s, a, "stock", 8, "user", booked_at=day_end)
        assert stock.count_article(s, a, "stock", 9, "user", booked_at=day_end) == 1  # against the first count
        assert s.get(Article, a).stock == 9
