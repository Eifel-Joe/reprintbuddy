"""p280: an order the shop cancelled is taken back, not marked as arrived (Spec 9.4).

The mark closes as taken back: what is still missing no longer counts as on its way, what has
arrived stays untouched, and a suggestion comes back when it is needed. Only the user takes back.
Filament is never booked (Spec 9.1): taking back writes no journal row, for parts neither.
"""

from datetime import datetime, timedelta

import pytest

from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.domain import filament, reorder, stock
from reprintbuddy.domain.errors import ConflictError, NotFoundError
from reprintbuddy.models import JournalEntry, ReorderMark

from test_reorder_suggestions import NOW, TODAY, filament_setup, part_with_demand, sorts_of, with_demand


def _journal_rows(s):
    return s.query(JournalEntry).count()


def test_taking_back_ends_on_its_way_and_the_suggestion_comes_back(db):
    with db.begin() as s:
        part = part_with_demand(s)
        mark = reorder.mark_ordered(s, "part", part.id, 2)
        assert reorder.suggestions(s, sorts=None, today=TODAY) == []
        rows = _journal_rows(s)
        withdrawn = reorder.withdraw_mark(s, mark.id)
        assert (mark.withdrawn, mark.received_at is not None) == (True, True)
        assert (withdrawn.item_type, withdrawn.packs, withdrawn.pack_size, withdrawn.arrived) == ("part", 2, 50, 0)
        assert withdrawn.supplier == "Example Supplier"
        assert reorder.on_way(s, None) == {}
        assert reorder.open_marks(s) == []
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert sug.item_id == part.id
        assert _journal_rows(s) == rows  # no booking
        reorder.mark_ordered(s, "part", part.id, 1)  # a new order is possible again


def test_after_a_partial_delivery_the_receipts_stay_and_only_the_rest_goes(db):
    """4 x 50 ordered, 100 in: taken back, the 100 stay in stock, 100 are no longer on their way."""
    with db.begin() as s:
        part = part_with_demand(s, stock=0)
        mark = reorder.mark_ordered(s, "part", part.id, 4)
        mark.marked_at -= timedelta(seconds=1)
        s.flush()
        stock.receive_part(s, part.id, 100, "user")
        assert reorder.on_way(s, None) == {("part", part.id): 100.0}
        withdrawn = reorder.withdraw_mark(s, mark.id)
        assert withdrawn.arrived == 100
        assert part.stock == 100
        assert reorder.on_way(s, None) == {}


def test_a_filament_order_is_taken_back_without_any_booking(db):
    """Spec 9.1: nothing is booked for filament; only the mark closes."""
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s, supplier="Example Shop")
        with_demand(s, sort)
        mark = reorder.mark_ordered(s, "filament", sort.id, 3)
        spool = fake.add_spool("PLA", brand="Brand A", created_at=mark.marked_at + timedelta(hours=1))
        filament.sync_sorts(s, [spool])
        rows = _journal_rows(s)
        withdrawn = reorder.withdraw_mark(s, mark.id, [spool])
        assert (withdrawn.item_type, withdrawn.packs, withdrawn.pack_size, withdrawn.arrived) == ("filament", 3, 1000, 1000)
        assert withdrawn.supplier == "Example Shop"
        assert _journal_rows(s) == rows
        assert reorder.on_way(s, [spool]) == {}
        assert [sug.item_id for sug in reorder.suggestions(s, sorts=sorts_of((sort, 100.0)), today=TODAY)] == [sort.id]


def test_a_closed_mark_cannot_be_taken_back(db):
    with db.begin() as s:
        part = part_with_demand(s)
        mark = reorder.mark_ordered(s, "part", part.id, 1)
        reorder.mark_received(s, mark.id)
        with pytest.raises(ConflictError) as arrived:
            reorder.withdraw_mark(s, mark.id)
        assert arrived.value.key == "reorder.already_received"
        assert mark.withdrawn is False
        other = reorder.mark_ordered(s, "part", part.id, 1)
        reorder.withdraw_mark(s, other.id)
        with pytest.raises(ConflictError) as twice:
            reorder.withdraw_mark(s, other.id)
        assert twice.value.key == "reorder.already_withdrawn"
        with pytest.raises(ConflictError) as late:
            reorder.mark_received(s, other.id)  # taken back is closed for "arrived" too
        assert late.value.key == "reorder.already_withdrawn"


def test_an_unknown_mark_is_not_found(db):
    with db.begin() as s, pytest.raises(NotFoundError) as info:
        reorder.withdraw_mark(s, 99)
    assert info.value.key == "reorder.mark_not_found"


def test_closing_by_rule_never_touches_a_mark_taken_back(db):
    """A receipt after taking back reaches the stock, but the mark stays taken back, not arrived."""
    with db.begin() as s:
        part = part_with_demand(s)
        mark = reorder.mark_ordered(s, "part", part.id, 1)
        mark.marked_at -= timedelta(seconds=1)
        s.flush()
        reorder.withdraw_mark(s, mark.id)
        closed_at = mark.received_at
        stock.receive_part(s, part.id, 50, "user")
        assert reorder.close_arrived_marks(s, None, NOW) == 0
        assert (mark.withdrawn, mark.received_at) == (True, closed_at)
        assert s.get(ReorderMark, mark.id).withdrawn is True
