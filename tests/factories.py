"""Test-only helpers. They set the counters directly, bypassing `domain/stock.py`."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy.models import (
    Article,
    Order,
    Part,
    ProductSet,
    SetPosition,
    SetPositionOption,
)


def make_article(
    s: Session,
    *,
    name: str = "Template A",
    library_file_id: int = 1,
    plate_id: int | None = None,
    **fields,
) -> Article:
    if plate_id is None:
        used = s.scalar(
            select(func.max(Article.plate_id)).where(Article.library_file_id == library_file_id)
        )
        plate_id = (used or 0) + 1
    fields.setdefault("file_name", f"{name}.gcode.3mf")
    article = Article(name=name, library_file_id=library_file_id, plate_id=plate_id, **fields)
    s.add(article)
    s.flush()
    return article


def make_part(s: Session, *, name: str = "Insert M4", **fields) -> Part:
    part = Part(name=name, **fields)
    s.add(part)
    s.flush()
    return part


def make_set(s: Session, name: str, positions: list[tuple[int, list[int]]]) -> ProductSet:
    """`positions` is a list of (qty, [article ids]); several ids make a position with a choice."""
    product_set = ProductSet(name=name)
    s.add(product_set)
    s.flush()
    for sort, (qty, article_ids) in enumerate(positions):
        position = SetPosition(set_id=product_set.id, sort=sort, qty=qty)
        s.add(position)
        s.flush()
        for article_id in article_ids:
            s.add(SetPositionOption(position_id=position.id, article_id=article_id))
    s.flush()
    return product_set


def make_order(s: Session, *, reference: str = "Order 1001", **fields) -> Order:
    """A bare order row (no lines, no reservation), for tests of the stock bookings."""
    fields.setdefault("order_date", date(2026, 1, 1))
    order = Order(reference=reference, **fields)
    s.add(order)
    s.flush()
    return order
