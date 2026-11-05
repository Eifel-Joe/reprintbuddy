"""Articles, purchased parts and sets (Spec 5). No stock changes happen here."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate, Printer
from reprintbuddy.domain import rules
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.domain.stock import MAX_QTY, Actor, count_part
from reprintbuddy.models import (
    Article,
    ArticleFilament,
    ArticlePart,
    Part,
    ProductSet,
    SetPosition,
    SetPositionOption,
)

MAX_NAME = 200
MAX_SKU = 64


# ---- validation helpers ----------------------------------------------------------------


def _int_in(value: object, field_key: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError("form.invalid_number", field=field_key, min=low, max=high)
    return value


def _text(value: str | None, field_key: str, max_len: int, *, required: bool = False) -> str | None:
    """Trimmed text; empty becomes None (or an error when required)."""
    value = (value or "").strip()
    if not value:
        if required:
            raise ValidationError("form.required", field=field_key)
        return None
    if len(value) > max_len:
        raise ValidationError("form.too_long", field=field_key, max=max_len)
    return value


def _required_name(value: str | None, field_key: str) -> str:
    name = _text(value, field_key, MAX_NAME, required=True)
    assert name is not None
    return name


def validate_sku(
    session: Session,
    sku: str | None,
    *,
    article_id: int | None = None,
    set_id: int | None = None,
) -> str | None:
    """Trimmed article number, unique over articles and sets together (both become SKUs later)."""
    sku = _text(sku, "catalog.field.sku", MAX_SKU)
    if sku is None:
        return None
    taken_by_article = session.scalar(select(Article.id).where(Article.sku == sku))
    taken_by_set = session.scalar(select(ProductSet.id).where(ProductSet.sku == sku))
    if (taken_by_article is not None and taken_by_article != article_id) or (
        taken_by_set is not None and taken_by_set != set_id
    ):
        raise ConflictError("catalog.sku_taken", sku=sku)
    return sku


# ---- print target (decision 2) -----------------------------------------------------------


@dataclass(frozen=True)
class TargetOption:
    value: str  # "model:<model>", "printer:<id>" or "" (no print target)
    model: str | None
    printer_name: str | None


def target_value(target_model: str | None, printer_id: int | None) -> str:
    if printer_id is not None:
        return f"printer:{printer_id}"
    if target_model:
        return f"model:{target_model}"
    return ""


def parse_target(value: str) -> tuple[str | None, int | None]:
    value = (value or "").strip()
    if value == "":
        return None, None
    if value.startswith("model:") and value[len("model:"):].strip():
        return value[len("model:"):].strip(), None
    match = re.fullmatch(r"printer:(\d{1,9})", value)
    if match and int(match.group(1)) >= 1:
        return None, int(match.group(1))
    raise ValidationError("articles.invalid_target")


def target_options(
    printers: Sequence[Printer] | None, file_model: str | None, current: str = ""
) -> list[TargetOption]:
    """The choices for the print target; `printers is None` means Bambuddy could not be asked."""
    try:
        current_model, _ = parse_target(current)
    except ValidationError:  # a stored or typed value in a wrong format is not offered
        current, current_model = "", None
    if printers is None:
        return [TargetOption(current, current_model, None)]
    models = sorted({p.model for p in printers} | ({file_model} if file_model else set()))
    options = [TargetOption(f"model:{m}", m, None) for m in models]
    options += [
        TargetOption(f"printer:{p.id}", p.model, p.name)
        for p in sorted(printers, key=lambda p: (p.name, p.id))
    ]
    if current and current not in {o.value for o in options}:
        options.append(TargetOption(current, current_model, None))
    options.append(TargetOption("", None, None))
    return options


# ---- articles ----------------------------------------------------------------------------


@dataclass
class ArticleInput:
    name: str
    sku: str | None = None
    units_per_print: int = 1
    min_stock: int = 0
    buffer: int = 0
    target_model: str | None = None
    printer_id: int | None = None
    needs_assembly: bool = False
    auto_enabled: bool = False
    parts: dict[int, int] = field(default_factory=dict)  # part id -> quantity per unit


_SUFFIXES = (".gcode.3mf", ".3mf", ".gcode")


def suggest_name(filename: str) -> str:
    lowered = filename.lower()
    for suffix in _SUFFIXES:
        if lowered.endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def default_article_input(file: LibraryFile) -> ArticleInput:
    """Spec 5.2 defaults: name from the file, print target is the model the file was sliced for."""
    return ArticleInput(name=suggest_name(file.filename), target_model=file.printer_model)


def _check_article_input(
    session: Session, data: ArticleInput, *, article_id: int | None = None
) -> ArticleInput:
    name = _required_name(data.name, "articles.field.name")
    sku = validate_sku(session, data.sku, article_id=article_id)
    target_model = _text(data.target_model, "articles.field.target", MAX_NAME)
    printer_id = data.printer_id
    if printer_id is not None:
        printer_id = _int_in(printer_id, "articles.field.printer_id", 1, MAX_QTY)
    if target_model is not None and printer_id is not None:
        raise ValidationError("articles.target_exclusive")
    parts: dict[int, int] = {}
    for part_id, qty in data.parts.items():
        parts[part_id] = _int_in(qty, "articles.field.qty_per_unit", 1, 1000)
        if session.get(Part, part_id) is None:
            raise NotFoundError("parts.not_found")
    return replace(
        data,
        name=name,
        sku=sku,
        units_per_print=_int_in(data.units_per_print, "articles.field.units_per_print", 1, 1000),
        min_stock=_int_in(data.min_stock, "articles.field.min_stock", 0, MAX_QTY),
        buffer=_int_in(data.buffer, "articles.field.buffer", 0, MAX_QTY),
        target_model=target_model,
        printer_id=printer_id,
        parts=parts,
    )


def _set_parts(session: Session, article_id: int, parts: dict[int, int]) -> None:
    session.execute(delete(ArticlePart).where(ArticlePart.article_id == article_id))
    for part_id, qty in parts.items():
        session.add(ArticlePart(article_id=article_id, part_id=part_id, qty_per_unit=qty))


def _apply_input(article: Article, data: ArticleInput) -> None:
    article.name = data.name
    article.sku = data.sku
    article.units_per_print = data.units_per_print
    article.min_stock = data.min_stock
    article.buffer = data.buffer
    article.target_model = data.target_model
    article.printer_id = data.printer_id
    article.needs_assembly = data.needs_assembly
    article.auto_enabled = data.auto_enabled


def create_article(
    session: Session,
    *,
    file: LibraryFile,
    plate: Plate,
    requirements: Sequence[FilamentRequirement],
    thumbnail: bytes | None,
    data: ArticleInput,
) -> Article:
    """Spec 5.1: an article from one plate of one print file. Creating is no booking."""
    data = _check_article_input(session, data)
    taken = session.scalar(
        select(Article).where(
            Article.library_file_id == file.id,
            Article.plate_id == plate.index,
            Article.archived_at.is_(None),
        )
    )
    if taken is not None:
        raise ConflictError("articles.plate_taken", name=taken.name)
    article = Article(
        library_file_id=file.id,
        plate_id=plate.index,
        file_name=file.filename,
        file_fingerprint=file.fingerprint,
        thumbnail=thumbnail,
    )
    _apply_input(article, data)
    session.add(article)
    session.flush()
    for requirement in requirements:
        session.add(
            ArticleFilament(
                article_id=article.id,
                slot=requirement.slot,
                grams=requirement.grams,
                material=requirement.material,
                color_hex=requirement.color_hex,
            )
        )
    _set_parts(session, article.id, data.parts)
    session.flush()
    return article


def update_article(session: Session, article_id: int, data: ArticleInput) -> Article:
    """Change the settings of an article; file and plate stay."""
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    data = _check_article_input(session, data, article_id=article_id)
    _apply_input(article, data)
    _set_parts(session, article.id, data.parts)
    session.flush()
    return article


@dataclass(frozen=True)
class ArticleRow:
    id: int
    name: str
    sku: str | None
    stock: int
    reserved: int
    available: int
    provisional: int
    awaiting_assembly: int
    min_stock: int
    target: int


def list_articles(session: Session) -> list[ArticleRow]:
    articles = session.scalars(select(Article)).all()
    rows = [
        ArticleRow(
            id=a.id,
            name=a.name,
            sku=a.sku,
            stock=a.stock,
            reserved=a.reserved,
            available=rules.available(a.stock, a.reserved),
            provisional=a.provisional,
            awaiting_assembly=a.awaiting_assembly,
            min_stock=a.min_stock,
            target=rules.target_quantity(a.min_stock, a.buffer),
        )
        for a in articles
    ]
    return sorted(rows, key=lambda r: (r.name.casefold(), r.id))


# ---- purchased parts -----------------------------------------------------------------------


@dataclass
class PartInput:
    name: str
    unit: str = "pcs"
    min_stock: int = 0
    pack_size: int = 1
    min_order_packs: int = 1
    lead_time_days: int = 0
    safety_value: int = 0
    safety_unit: Literal["days", "qty"] = "days"
    supplier: str | None = None
    order_url: str | None = None


def _check_part_input(data: PartInput) -> PartInput:
    unit = _text(data.unit, "parts.field.unit", 20, required=True)
    if data.safety_unit not in ("days", "qty"):
        raise ValidationError("parts.invalid_safety_unit")
    url = _text(data.order_url, "parts.field.order_url", 2000)
    if url is not None and not url.lower().startswith(("http://", "https://")):
        raise ValidationError("parts.invalid_url")
    return replace(
        data,
        name=_required_name(data.name, "parts.field.name"),
        unit=unit,
        min_stock=_int_in(data.min_stock, "parts.field.min_stock", 0, MAX_QTY),
        pack_size=_int_in(data.pack_size, "parts.field.pack_size", 1, MAX_QTY),
        min_order_packs=_int_in(data.min_order_packs, "parts.field.min_order_packs", 1, 1000),
        lead_time_days=_int_in(data.lead_time_days, "parts.field.lead_time_days", 0, 365),
        safety_value=_int_in(data.safety_value, "parts.field.safety_value", 0, MAX_QTY),
        supplier=_text(data.supplier, "parts.field.supplier", MAX_NAME),
        order_url=url,
    )


def _apply_part(part: Part, data: PartInput) -> None:
    part.name = data.name
    part.unit = data.unit
    part.min_stock = data.min_stock
    part.pack_size = data.pack_size
    part.min_order_packs = data.min_order_packs
    part.lead_time_days = data.lead_time_days
    part.safety_value = data.safety_value
    part.safety_unit = data.safety_unit
    part.supplier = data.supplier
    part.order_url = data.order_url


def create_part(session: Session, data: PartInput, initial_stock: int, actor: Actor) -> Part:
    """New purchased part; the initial stock is booked as an inventory count (Spec 5.3, 6.5)."""
    data = _check_part_input(data)
    initial_stock = _int_in(initial_stock, "parts.field.initial_stock", 0, MAX_QTY)
    part = Part()
    _apply_part(part, data)
    session.add(part)
    session.flush()
    if initial_stock > 0:
        count_part(session, part.id, initial_stock, actor)
    return part


def update_part(session: Session, part_id: int, data: PartInput) -> Part:
    """Change the settings of a part; its stock only changes through bookings."""
    part = session.get(Part, part_id)
    if part is None:
        raise NotFoundError("parts.not_found")
    _apply_part(part, _check_part_input(data))
    session.flush()
    return part


def list_parts(session: Session) -> list[Part]:
    parts = session.scalars(select(Part)).all()
    return sorted(parts, key=lambda p: (p.name.casefold(), p.id))


# ---- sets ----------------------------------------------------------------------------------


@dataclass
class PositionInput:
    qty: int
    article_ids: list[int]  # one article: fixed; several: a choice (Spec 5.4)


@dataclass
class SetInput:
    name: str
    sku: str | None
    positions: list[PositionInput]


@dataclass(frozen=True)
class PositionRow:
    id: int
    qty: int
    article_ids: list[int]
    article_names: list[str]
    is_choice: bool


@dataclass(frozen=True)
class SetRow:
    id: int
    name: str
    sku: str | None
    available: int
    positions: list[PositionRow]


def _check_set_input(
    session: Session, data: SetInput, *, set_id: int | None = None
) -> tuple[str, str | None, list[tuple[int, list[int]]]]:
    name = _required_name(data.name, "sets.field.name")
    sku = validate_sku(session, data.sku, set_id=set_id)
    if not data.positions:
        raise ValidationError("sets.no_positions")
    positions: list[tuple[int, list[int]]] = []
    for position in data.positions:
        qty = _int_in(position.qty, "sets.field.qty", 1, 1000)
        article_ids = list(dict.fromkeys(position.article_ids))
        if not article_ids:
            raise ValidationError("sets.position_empty")
        for article_id in article_ids:
            if session.get(Article, article_id) is None:
                raise NotFoundError("articles.not_found")
        positions.append((qty, article_ids))
    return name, sku, positions


def _write_positions(
    session: Session, set_id: int, positions: list[tuple[int, list[int]]]
) -> None:
    for sort, (qty, article_ids) in enumerate(positions):
        position = SetPosition(set_id=set_id, sort=sort, qty=qty)
        session.add(position)
        session.flush()
        for article_id in article_ids:
            session.add(SetPositionOption(position_id=position.id, article_id=article_id))
    session.flush()


def create_set(session: Session, data: SetInput) -> ProductSet:
    name, sku, positions = _check_set_input(session, data)
    product_set = ProductSet(name=name, sku=sku)
    session.add(product_set)
    session.flush()
    _write_positions(session, product_set.id, positions)
    return product_set


def get_set(session: Session, set_id: int) -> ProductSet:
    product_set = session.get(ProductSet, set_id)
    if product_set is None:
        raise NotFoundError("sets.not_found")
    return product_set


def update_set(session: Session, set_id: int, data: SetInput) -> ProductSet:
    """Replace name, number and all positions. Open orders keep their own lines."""
    product_set = get_set(session, set_id)
    name, sku, positions = _check_set_input(session, data, set_id=set_id)
    old = session.scalars(select(SetPosition.id).where(SetPosition.set_id == set_id)).all()
    if old:
        session.execute(delete(SetPositionOption).where(SetPositionOption.position_id.in_(old)))
        session.execute(delete(SetPosition).where(SetPosition.id.in_(old)))
    product_set.name = name
    product_set.sku = sku
    _write_positions(session, set_id, positions)
    return product_set


def _set_row(session: Session, product_set: ProductSet) -> SetRow:
    positions = session.scalars(
        select(SetPosition).where(SetPosition.set_id == product_set.id).order_by(SetPosition.sort, SetPosition.id)
    ).all()
    rows: list[PositionRow] = []
    stocks: list[rules.PositionStock] = []
    for position in positions:
        articles = session.scalars(
            select(Article)
            .join(SetPositionOption, SetPositionOption.article_id == Article.id)
            .where(SetPositionOption.position_id == position.id)
            .order_by(Article.name, Article.id)
        ).all()
        rows.append(
            PositionRow(
                id=position.id,
                qty=position.qty,
                article_ids=[a.id for a in articles],
                article_names=[a.name for a in articles],
                is_choice=len(articles) > 1,
            )
        )
        stocks.append(
            rules.PositionStock(
                position.qty, tuple(rules.available(a.stock, a.reserved) for a in articles)
            )
        )
    return SetRow(
        id=product_set.id,
        name=product_set.name,
        sku=product_set.sku,
        available=rules.set_available(stocks),
        positions=rows,
    )


def list_sets(session: Session) -> list[SetRow]:
    sets = sorted(session.scalars(select(ProductSet)).all(), key=lambda s: (s.name.casefold(), s.id))
    return [_set_row(session, s) for s in sets]
