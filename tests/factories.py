"""Test-only helpers. They set the counters directly, bypassing `domain/stock.py`."""

from __future__ import annotations

from collections.abc import Sequence

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy.domain import filament
from reprintbuddy.models import (
    Article,
    ArticleComponent,
    ArticleFilament,
    ArticleFilamentSort,
    ArticleOption,
    Dispatch,
    FilamentSettings,
    FilamentSnapshot,
    FilamentSort,
    FilamentSupplier,
    Order,
    Part,
    PartSupplier,
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


def make_assembled(
    s: Session,
    *,
    name: str = "Storage box",
    sku: str | None = None,
    mode: str = "stock",
    base: Sequence[tuple[int, int]] = (),
    fields: Sequence[Sequence[tuple[str, Sequence[tuple[int, int]]]]] = (),
    **extra,
) -> Article:
    """An article made of parts (0.4.0, Spec 5.6): base parts as (part_id, qty), and up to two choice
    fields, each a list of options (suffix, [(part_id, qty)]). Its variant rows come from
    `assembled.sync_variants`, one per combination of options. No checks: tests build odd ones too."""
    article = Article(name=name, sku=sku, kind="assembled", assembly_mode=mode, **extra)
    s.add(article)
    s.flush()
    for sort, (part_id, qty) in enumerate(base):
        s.add(ArticleComponent(article_id=article.id, field=0, part_id=part_id, qty=qty, sort=sort))
    per_field: list[list[ArticleOption]] = []
    for field_no, options in enumerate(fields, start=1):
        made = []
        for sort, (suffix, parts) in enumerate(options):
            option = ArticleOption(article_id=article.id, field=field_no, sort=sort, suffix=suffix)
            s.add(option)
            s.flush()
            for part_sort, (part_id, qty) in enumerate(parts):
                s.add(ArticleComponent(
                    article_id=article.id, field=field_no, option_id=option.id, part_id=part_id, qty=qty,
                    sort=part_sort,
                ))
            made.append(option)
        per_field.append(made)
    s.flush()
    from reprintbuddy.domain import assembled  # the rows as the program makes them (0.4.0 task 3)

    assembled.sync_variants(s, article)
    s.flush()
    return article


SUPPLIER_FIELDS = {"pack_size": "pack_size", "min_order_packs": "min_order_packs",
                   "lead_time_days": "lead_time_days", "supplier": "name", "order_url": "order_url"}


def make_part(s: Session, *, name: str = "Insert M4", **fields) -> Part:
    """A part with one default supplier; the supplier fields a part had before 0015
    (`pack_size`, `min_order_packs`, `lead_time_days`, `supplier`, `order_url`) go to it."""
    supplier = {SUPPLIER_FIELDS[key]: fields.pop(key) for key in list(fields) if key in SUPPLIER_FIELDS}
    part = Part(name=name, **fields)
    s.add(part)
    s.flush()
    s.add(PartSupplier(part_id=part.id, position=0, is_default=True, **supplier))
    s.flush()
    return part


def make_set(
    s: Session,
    name: str,
    base: list[tuple[int, int]],
    choices: Sequence[tuple[int, list[tuple[int, str | None]]]] = (),
    *,
    sku: str | None = None,
) -> ProductSet:
    """`base` is a list of (qty, article id); `choices` up to two fields of (qty, [(article id, suffix)]).
    Nothing is checked: an update or an older version may have left a set like this (Spec 5.4)."""
    product_set = ProductSet(name=name, sku=sku)
    s.add(product_set)
    s.flush()
    for sort, (qty, article_id) in enumerate(base):
        position = SetPosition(set_id=product_set.id, sort=sort, qty=qty, slot=0)
        s.add(position)
        s.flush()
        s.add(SetPositionOption(position_id=position.id, article_id=article_id))
    for slot, (qty, options) in enumerate(choices, start=1):
        position = SetPosition(set_id=product_set.id, sort=0, qty=qty, slot=slot)
        s.add(position)
        s.flush()
        for sort, (article_id, suffix) in enumerate(options):
            s.add(SetPositionOption(position_id=position.id, article_id=article_id, suffix=suffix, sort=sort))
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


FILAMENT_SUPPLIER_FIELDS = {"pack_grams": "pack_size", "min_order_packs": "min_order_packs",
                            "lead_time_days": "lead_time_days", "supplier": "name", "order_url": "order_url"}


def make_filament_settings(s: Session, sort_id: int, **fields) -> FilamentSettings:
    """Order settings with one default supplier; the supplier fields the settings had before 0016
    (`pack_grams`, `min_order_packs`, `lead_time_days`, `supplier`, `order_url`) go to it."""
    supplier = {FILAMENT_SUPPLIER_FIELDS[key]: fields.pop(key) for key in list(fields) if key in FILAMENT_SUPPLIER_FIELDS}
    settings = FilamentSettings(spool_filament_id=sort_id, **fields)
    s.add(settings)
    s.flush()
    s.add(FilamentSupplier(sort_id=sort_id, position=0, is_default=True, **supplier))
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
