"""What counts as "new" on the overview (Spec 10.2): since /orders or /reorder was last loaded.

One account, one shared mark for every device. Never loaded: nothing is new. The marks are internal
settings; writing one that would not change is left out, so loading a page with nothing new writes
nothing (each write wakes every page that follows the settings).
"""

from types import SimpleNamespace

from factories import make_order
from reprintbuddy import settings as st
from reprintbuddy.domain import seen
from reprintbuddy.models import Setting


def suggestion(kind, item_id):
    return SimpleNamespace(item_type=kind, item_id=item_id)


def test_orders_never_loaded_nothing_new(db):
    with db.begin() as s:
        ids = [make_order(s, reference=f"R{n}").id for n in range(3)]
        assert seen.new_orders(s, ids) == 0


def test_orders_created_after_the_last_load_are_new(db):
    with db.begin() as s:
        first = make_order(s, reference="R1").id
        assert seen.mark_orders(s) is True
    with db.begin() as s:
        second = make_order(s, reference="R2").id
        third = make_order(s, reference="R3").id
        assert seen.new_orders(s, [first, second, third]) == 2
        assert seen.new_orders(s, [first, third]) == 1  # only the open ones the card counts
    with db.begin() as s:
        assert seen.mark_orders(s) is True
        assert seen.new_orders(s, [first, second, third]) == 0


def test_loading_again_without_a_new_order_writes_nothing(db):
    with db.begin() as s:
        make_order(s)
        assert seen.mark_orders(s) is True
    with db.begin() as s:
        assert seen.mark_orders(s) is False
    with db.begin() as s:
        assert seen.mark_orders(s) is False


def test_no_order_at_all_marks_zero_once(db):
    with db.begin() as s:
        assert seen.mark_orders(s) is True  # loaded once: from now on a new order is new
        assert st.get_setting(s, "orders_seen_id") == 0
    with db.begin() as s:
        assert seen.mark_orders(s) is False
        new = make_order(s).id
        assert seen.new_orders(s, [new]) == 1


def test_suggestions_never_loaded_nothing_new(db):
    with db.begin() as s:
        assert seen.new_suggestions(s, [suggestion("part", 1)]) == 0


def test_suggestions_not_shown_at_the_last_load_are_new(db):
    with db.begin() as s:
        assert seen.mark_suggestions(s, [suggestion("part", 1), suggestion("filament", 1)]) is True
    with db.begin() as s:
        now = [suggestion("part", 1), suggestion("filament", 1), suggestion("filament", 2), suggestion("part", 9)]
        assert seen.new_suggestions(s, now) == 2
        assert seen.mark_suggestions(s, now) is True
        assert seen.new_suggestions(s, now) == 0
    with db.begin() as s:
        assert seen.mark_suggestions(s, list(reversed(now))) is False  # same set, nothing written


def test_the_marks_are_internal_settings(db):
    from reprintbuddy.activity import values

    assert {"orders_seen_id", "reorder_seen"} <= values.INTERNAL
    with db.begin() as s:
        seen.mark_suggestions(s, [suggestion("part", 3), suggestion("filament", 2)])
    with db.begin() as s:
        assert st.get_setting(s, "reorder_seen") == ["filament:2", "part:3"]
        assert s.get(Setting, "reorder_seen") is not None


def test_orders_mark_due_reads_only(db):
    with db.begin() as s:
        assert seen.orders_mark_due(s) is True  # never loaded
        seen.mark_orders(s)
        assert seen.orders_mark_due(s) is False
        make_order(s)
        assert seen.orders_mark_due(s) is True
