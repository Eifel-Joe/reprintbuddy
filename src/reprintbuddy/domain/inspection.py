"""Inspections of finished prints (Spec 8.1, plan B4 decision 18).

A finished print books its units as provisional and opens an inspection in the same
transaction; B5 builds the count of good pieces on it. Functions never commit.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from reprintbuddy.domain.stock import add_provisional
from reprintbuddy.models import Dispatch, Inspection


def open_inspection(session: Session, dispatch: Dispatch) -> Inspection:
    """Units of the finished `dispatch` become provisional, waiting for the inspection."""
    inspection = Inspection(
        dispatch_id=dispatch.id, article_id=dispatch.article_id, units=dispatch.units
    )
    session.add(inspection)
    session.flush()
    add_provisional(
        session, dispatch.article_id, dispatch.units, "auto", details={"dispatch_id": dispatch.id}
    )
    return inspection
