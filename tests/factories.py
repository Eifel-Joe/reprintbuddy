"""Test-only helpers. They set the counters directly, bypassing `domain/stock.py`."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy.domain import filament
from reprintbuddy.models import (
    Article,
    ArticleFilament,
    ArticleFilamentSort,
    Dispatch,
    FilamentSettings,
    FilamentSnapshot,
    FilamentSort,
    Order,
    Part,
    ProductSet,
    SetPosition,
    SetPositionOption,
    WorkQueueEntry,
    utcnow,
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


def make_entry(
    s: Session, article_id: int, prints: int = 1, *, origin: str = "rule", **fields
) -> WorkQueueEntry:
    """A work queue entry at the end of the queue (position = highest so far + 1)."""
    if "position" not in fields:
        fields["position"] = (s.scalar(select(func.max(WorkQueueEntry.position))) or 0) + 1
    entry = WorkQueueEntry(article_id=article_id, prints=prints, origin=origin, **fields)
    s.add(entry)
    s.flush()
    return entry


def make_dispatch(
    s: Session,
    article_id: int,
    *,
    status: str = "queued",
    units: int = 1,
    origin: str = "rule",
    **fields,
) -> Dispatch:
    fields.setdefault("queue_position", 0)
    dispatch = Dispatch(article_id=article_id, status=status, units=units, origin=origin, **fields)
    s.add(dispatch)
    s.flush()
    return dispatch


def make_sort(
    s: Session,
    *,
    material: str = "PLA",
    color_hex: str | None = "#000000",
    subtype: str | None = None,
    color_name: str | None = "Black",
    brand: str | None = "Example Brand",  # the default brand of a fake spool: same key
) -> FilamentSort:
    """A spool sort with key and label from `domain/filament.py` (one place for the rules)."""
    sort = FilamentSort(
        sort_key=filament.sort_key(brand, material, subtype, color_hex),
        brand=brand,
        material=material,
        subtype=subtype,
        color_hex=color_hex,
        color_name=color_name,
        label=filament.sort_label(brand, material, subtype, color_name, color_hex),
    )
    s.add(sort)
    s.flush()
    return sort


def make_legacy_sort(
    s: Session,
    *,
    material: str = "PLA",
    color_hex: str | None = "#000000",
    subtype: str | None = None,
    color_name: str | None = "Black",
) -> FilamentSort:
    """A brandless sort of 0006 as migration 0007 leaves it: `legacy`, key `legacy:<id>`."""
    sort = FilamentSort(
        sort_key=f"legacy:{uuid.uuid4().hex}",
        legacy=True,
        brand=None,
        material=material,
        subtype=subtype,
        color_hex=color_hex,
        color_name=color_name,
        label=filament.sort_label(None, material, subtype, color_name, color_hex),
    )
    s.add(sort)
    s.flush()
    sort.sort_key = f"legacy:{sort.id}"
    s.flush()
    return sort


def make_snapshot(
    s: Session, day: date, spool_id: int, sort_id: int, grams: float, **fields
) -> FilamentSnapshot:
    fields.setdefault("label_g", 1000)
    snapshot = FilamentSnapshot(
        day=day, spool_id=spool_id, spool_filament_id=sort_id, remaining_g=grams, **fields
    )
    s.add(snapshot)
    s.flush()
    return snapshot


def make_filament_settings(s: Session, sort_id: int, **fields) -> FilamentSettings:
    settings = FilamentSettings(spool_filament_id=sort_id, **fields)
    s.add(settings)
    s.flush()
    return settings


def tick_sorts(s: Session, article_id: int, slot: int, *sorts: FilamentSort, reordered: FilamentSort | None = None) -> None:
    """Tick `sorts` for one slot of an article; the first is reordered unless `reordered` names one."""
    chosen = reordered if reordered is not None else (sorts[0] if sorts else None)
    for sort in sorts:
        s.add(ArticleFilamentSort(article_id=article_id, slot=slot, sort_id=sort.id, reordered=sort is chosen))
    s.flush()


def assign_sort(s: Session, sort: FilamentSort, *, name: str = "Assigned article", archived: bool = False,
                grams: float = 10.0, **fields) -> Article:
    """An article with one slot (material and colour of `sort`) where `sort` is ticked and reordered."""
    article = make_article(s, name=name, **fields)
    s.add(ArticleFilament(article_id=article.id, slot=1, grams=grams, material=sort.material, color_hex=sort.color_hex))
    s.flush()
    tick_sorts(s, article.id, 1, sort)
    if archived:
        article.archived_at = utcnow()
    s.flush()
    return article
