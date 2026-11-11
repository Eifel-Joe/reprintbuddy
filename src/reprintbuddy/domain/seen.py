"""What is "new" on the overview (Spec 10.2, 0.4.1): since /orders or /reorder was last loaded.

Reprintbuddy has one account, so the mark is one for every device: looking at the orders on the phone
clears "new" on the PC too. Only loading the page counts, never the live refetch of its regions (a window
left open would otherwise swallow every "new"); the routes decide that, this module keeps the marks.

The marks are internal settings (not in the activity log, on no settings page). A mark that would not
change is not written: every write of a setting wakes the pages that follow the settings.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.models import Order


class _Suggestion(Protocol):
    item_type: str
    item_id: int


def _keys(suggestions: Iterable[_Suggestion]) -> list[str]:
    return sorted({f"{s.item_type}:{s.item_id}" for s in suggestions})


def _highest_order(session: Session) -> int:
    return int(session.scalar(select(func.max(Order.id))) or 0)


def orders_mark_due(session: Session) -> bool:
    """Would loading /orders change the mark? Read only: the page takes the write lock only then
    (review v0.4.1 B, R1: the open orders used to only read)."""
    return st.get_setting(session, "orders_seen_id") != _highest_order(session)


def mark_orders(session: Session) -> bool:
    """/orders was loaded: every order so far is seen. True when the mark changed (and was written)."""
    highest = _highest_order(session)
    if st.get_setting(session, "orders_seen_id") == highest:
        return False
    st.set_setting(session, "orders_seen_id", highest, commit=False)
    return True


def new_orders(session: Session, order_ids: Sequence[int]) -> int:
    """How many of `order_ids` (the open orders the card counts) came after the last load; 0 if never loaded."""
    mark = st.get_setting(session, "orders_seen_id")
    if mark is None:
        return 0
    return sum(1 for order_id in order_ids if order_id > mark)


def mark_suggestions(session: Session, suggestions: Iterable[_Suggestion]) -> bool:
    """/reorder was loaded showing `suggestions`. True when the mark changed (and was written)."""
    keys = _keys(suggestions)
    if st.get_setting(session, "reorder_seen") == keys:
        return False
    st.set_setting(session, "reorder_seen", keys, commit=False)
    return True


def new_suggestions(session: Session, suggestions: Iterable[_Suggestion]) -> int:
    """How many of `suggestions` /reorder did not show when it was last loaded; 0 if never loaded."""
    mark = st.get_setting(session, "reorder_seen")
    if mark is None:
        return 0
    shown = set(mark)
    return sum(1 for key in _keys(suggestions) if key not in shown)
