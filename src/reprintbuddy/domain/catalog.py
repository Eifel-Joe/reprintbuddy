"""Articles, purchased parts and sets (Spec 5). No stock changes happen here."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.activity.capture import quiet
from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate, Printer
from reprintbuddy.domain import rules, workqueue
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.domain.workqueue import OPEN_STATES
from reprintbuddy.domain.status import ArticleStatus, article_status
from reprintbuddy.domain.status import SetStatus, set_status
from reprintbuddy.domain.stock import MAX_QTY, Actor, count_part, write_journal
from reprintbuddy.models import (
    Article,
    ArticleFilament,
    ArticleFilamentSort,
    ArticlePart,
    Dispatch,
    Order,
    OrderLine,
    Part,
    PartSupplier,
    ProductSet,
    SetPosition,
    SetPositionOption,
    WorkQueueEntry,
    utcnow,
)

MAX_NAME = 200
MAX_SKU = 64
MAX_LABEL_TEXT = 60  # the text for the label (Spec 5.2): a label has little room


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
    # A printer in maintenance is only temporarily away: not offered anew (neither alone nor
    # through its model), but a target that already names it keeps its name and model.
    offered = [p for p in printers if p.active]
    models = sorted({p.model for p in offered} | ({file_model} if file_model else set()))
    options = [TargetOption(f"model:{m}", m, None) for m in models]
    options += [
        TargetOption(f"printer:{p.id}", p.model, p.name)
        for p in sorted(offered, key=lambda p: (p.name, p.id))
    ]
    if current and current not in {o.value for o in options}:
        known = next((p for p in printers if f"printer:{p.id}" == current), None)
        if known is not None:
            options.append(TargetOption(current, known.model, known.name))
        else:
            options.append(TargetOption(current, current_model, None))
    options.append(TargetOption("", None, None))
    return options


# ---- articles ----------------------------------------------------------------------------


@dataclass
class ArticleInput:
    name: str
    sku: str | None = None
    label_text: str | None = None
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


def _linked_parts(session: Session, article_id: int | None) -> dict[int, int]:
    """The parts an article already uses (part id -> quantity per unit)."""
    if article_id is None:
        return {}
    rows = session.execute(
        select(ArticlePart.part_id, ArticlePart.qty_per_unit).where(ArticlePart.article_id == article_id)
    ).all()
    return {part_id: qty for part_id, qty in rows}


def _check_article_input(
    session: Session, data: ArticleInput, *, article_id: int | None = None
) -> ArticleInput:
    name = _required_name(data.name, "articles.field.name")
    sku = validate_sku(session, data.sku, article_id=article_id)
    label_text = _text(data.label_text, "articles.field.label_text", MAX_LABEL_TEXT)
    target_model = _text(data.target_model, "articles.field.target", MAX_NAME)
    printer_id = data.printer_id
    if printer_id is not None:
        printer_id = _int_in(printer_id, "articles.field.printer_id", 1, MAX_QTY)
    if target_model is not None and printer_id is not None:
        raise ValidationError("articles.target_exclusive")
    # The same rule as the switch of the list (`set_auto_enabled`): the automation needs a print
    # target, `dispatch.reserve_next` would pause the article with `no_target` at once (p52).
    if data.auto_enabled and target_model is None and printer_id is None:
        raise ValidationError("articles.field_auto_needs_target", field="articles.field.auto_enabled")
    parts: dict[int, int] = {}
    linked = _linked_parts(session, article_id)
    for part_id, qty in data.parts.items():
        parts[part_id] = _int_in(qty, "articles.field.qty_per_unit", 1, 1000)
        part = session.get(Part, part_id)
        if part is None:
            raise NotFoundError("parts.not_found")
        if part.archived_at is not None and part_id not in linked:
            raise ValidationError("parts.archived", name=part.name)
    return replace(
        data,
        name=name,
        sku=sku,
        label_text=label_text,
        units_per_print=_int_in(data.units_per_print, "articles.field.units_per_print", 1, 1000),
        min_stock=_int_in(data.min_stock, "articles.field.min_stock", 0, MAX_QTY),
        buffer=_int_in(data.buffer, "articles.field.buffer", 0, MAX_QTY),
        target_model=target_model,
        printer_id=printer_id,
        parts=parts,
    )


def _delete_rows(session: Session, query) -> None:
    """Delete row by row, so the change feed can name each row (changes.py). The flush
    clears the way for new rows with the same key (ArticlePart: article and part)."""
    for row in session.scalars(query).all():
        session.delete(row)
    session.flush()


def _set_parts(session: Session, article_id: int, parts: dict[int, int]) -> None:
    _delete_rows(session, select(ArticlePart).where(ArticlePart.article_id == article_id))
    for part_id, qty in parts.items():
        session.add(ArticlePart(article_id=article_id, part_id=part_id, qty_per_unit=qty))


def _apply_input(article: Article, data: ArticleInput) -> None:
    article.name = data.name
    article.sku = data.sku
    article.label_text = data.label_text
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
    if article.archived_at is not None:
        raise ConflictError("articles.edit_archived")
    linked = _linked_parts(session, article_id)
    data = _check_article_input(session, data, article_id=article_id)
    # The form only offers active parts: a link to an archived part is not the user's to drop.
    archived_links = {
        part_id: qty
        for part_id, qty in linked.items()
        if part_id not in data.parts and session.get(Part, part_id).archived_at is not None
    }
    _apply_input(article, data)
    _set_parts(session, article.id, {**archived_links, **data.parts})
    session.flush()
    return article


def set_target_quantity(session: Session, article_id: int, target: int) -> Article:
    """Set the target quantity of an article from the list or a tile (Spec 10.1, p154).

    The target quantity is minimum stock + buffer (Spec 4), so the new target sets the minimum stock
    to target - buffer; the buffer stays. A target below the buffer cannot be: the minimum stock would
    be negative. Same checks and same transaction as the edit form (`update_article`: number range,
    archived article refused), and like it no journal entry (the journal is for stock bookings).
    Rule 7.1 reads the minimum stock on its next look, as after any edit.

    The buffer is read here, in the transaction that writes: a buffer changed in the meantime on
    another device cannot make the minimum stock negative, the target is then refused with the buffer
    it is measured against. The value sent last wins, as with the edit form.
    """
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    if article.archived_at is not None:
        raise ConflictError("articles.edit_archived")
    target = _int_in(target, "articles.target_quantity", 0, MAX_QTY)
    if target < article.buffer:
        raise ValidationError("articles.target_below_buffer", buffer=article.buffer)
    minimum = target - article.buffer
    if article.min_stock != minimum:
        article.min_stock = minimum
        session.flush()
    return article


def set_auto_enabled(session: Session, article_id: int, enabled: bool) -> Article:
    """Switch the automation of one article on or off (the switch of the list, p44).

    The wanted state is passed, not "toggle": two open tabs cannot turn each other back.
    Off always works for an active article. On needs what the automation needs: a print target
    (`dispatch.reserve_next` pauses an article without one with `no_target`). An archived article
    has no automation. The edit form writes no journal entry for this field, so neither does this.
    """
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    if article.archived_at is not None:
        raise ConflictError("articles.auto_archived", name=article.name)
    if enabled and not (article.target_model or article.printer_id is not None):
        raise ConflictError("articles.auto_needs_target", name=article.name)
    if article.auto_enabled != enabled:
        article.auto_enabled = enabled
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
    has_thumbnail: bool
    paused: bool
    coverage: int
    status: ArticleStatus
    queued_units: int
    in_progress_units: int
    in_progress_prints: int  # open prints in Bambuddy: the note "N in Bambuddy" (Spec 10.1)
    auto_enabled: bool = False  # the switch of the list (p44)
    buffer: int = 0  # the field of the target quantity stops at it (p154)


def article_row(
    article: Article, *, queued_units: int = 0, in_progress_units: int = 0, in_progress_prints: int = 0
) -> ArticleRow:
    """The numbers and the state of one article, as the overview shows them.

    The caller passes what the queue holds (`workqueue.coverage_all` / `coverage_of`), so the
    status follows the full coverage of Spec 7.1 (Spec 10.1). Without them the coverage
    leaves out the queue and the open prints.
    """
    available = rules.available(article.stock, article.reserved)
    coverage = rules.coverage(
        available, queued_units, in_progress_units, article.provisional, article.awaiting_assembly
    )
    return ArticleRow(
        id=article.id,
        name=article.name,
        sku=article.sku,
        stock=article.stock,
        reserved=article.reserved,
        available=available,
        provisional=article.provisional,
        awaiting_assembly=article.awaiting_assembly,
        min_stock=article.min_stock,
        target=rules.target_quantity(article.min_stock, article.buffer),
        has_thumbnail=bool(article.thumbnail),
        paused=article.paused,
        coverage=coverage,
        status=article_status(available=available, min_stock=article.min_stock, coverage=coverage),
        queued_units=queued_units,
        in_progress_units=in_progress_units,
        in_progress_prints=in_progress_prints,
        auto_enabled=article.auto_enabled,
        buffer=article.buffer,
    )


def _archive_filter(column, archived: bool):
    return column.is_not(None) if archived else column.is_(None)


def list_articles(session: Session, *, archived: bool = False) -> list[ArticleRow]:
    """Active articles; with `archived=True` only the archived ones (Spec 5.5)."""
    articles = session.scalars(select(Article).where(_archive_filter(Article.archived_at, archived))).all()
    # one query per table; an archived article has no entry and no open print (plan B4 decision 20)
    coverages = {} if archived else workqueue.coverage_all(session)
    prints = {} if archived else workqueue.in_progress_prints(session)
    rows = []
    for a in articles:
        c = coverages.get(a.id)
        rows.append(
            article_row(
                a,
                queued_units=c.queued_units if c else 0,
                in_progress_units=c.in_progress_units if c else 0,
                in_progress_prints=prints.get(a.id, 0),
            )
        )
    return sorted(rows, key=lambda r: (r.name.casefold(), r.id))


# ---- purchased parts -----------------------------------------------------------------------


MAX_SUPPLIERS = 10  # per part (Spec 5.3)
SUPPLIER_COLUMNS = ("name", "order_url", "pack_size", "min_order_packs", "lead_time_days", "is_default")


@dataclass
class SupplierInput:
    """One supplier of a part (Spec 5.3): where it is bought, in which packs, how fast."""

    name: str | None = None
    order_url: str | None = None
    pack_size: int = 1
    min_order_packs: int = 1
    lead_time_days: int = 0
    is_default: bool = False


def _one_default() -> list[SupplierInput]:
    return [SupplierInput(is_default=True)]


@dataclass
class PartInput:
    name: str
    unit: str = "pcs"
    min_stock: int = 0
    safety_value: int = 0
    safety_unit: Literal["days", "qty"] = "days"
    # in the order of the form; a part has 1 to 10, exactly one the default (Spec 5.3)
    suppliers: list[SupplierInput] = field(default_factory=_one_default)


def _check_supplier(data: SupplierInput, number: int) -> SupplierInput:
    """One supplier's fields; an error names the supplier by its number (1 for the first)."""
    try:
        url = _text(data.order_url, "parts.field.order_url", 2000)
        if url is not None and not url.lower().startswith(("http://", "https://")):
            raise ValidationError("parts.invalid_url")
        return SupplierInput(
            name=_text(data.name, "parts.field.supplier", MAX_NAME),
            order_url=url,
            pack_size=_int_in(data.pack_size, "parts.field.pack_size", 1, MAX_QTY),
            min_order_packs=_int_in(data.min_order_packs, "parts.field.min_order_packs", 1, 1000),
            lead_time_days=_int_in(data.lead_time_days, "parts.field.lead_time_days", 0, 365),
            is_default=bool(data.is_default),
        )
    except ValidationError as error:
        error.params["supplier"] = number
        raise


def _check_part_input(data: PartInput) -> PartInput:
    unit = _text(data.unit, "parts.field.unit", 20, required=True)
    if data.safety_unit not in ("days", "qty"):
        raise ValidationError("parts.invalid_safety_unit")
    if not 1 <= len(data.suppliers) <= MAX_SUPPLIERS:
        raise ValidationError("parts.suppliers_count", max=MAX_SUPPLIERS)
    suppliers = [_check_supplier(item, number) for number, item in enumerate(data.suppliers, start=1)]
    if sum(item.is_default for item in suppliers) != 1:
        raise ValidationError("parts.supplier_default")
    return replace(
        data,
        name=_required_name(data.name, "parts.field.name"),
        unit=unit,
        min_stock=_int_in(data.min_stock, "parts.field.min_stock", 0, MAX_QTY),
        safety_value=_int_in(data.safety_value, "parts.field.safety_value", 0, MAX_QTY),
        suppliers=suppliers,
    )


def _apply_part(part: Part, data: PartInput) -> None:
    part.name = data.name
    part.unit = data.unit
    part.min_stock = data.min_stock
    part.safety_value = data.safety_value
    part.safety_unit = data.safety_unit


def _apply_suppliers(session: Session, part_id: int, wanted: list[SupplierInput]) -> None:
    """Make the part's suppliers what the form says, compared by position.

    Unchanged rows are not touched, so saving the same values writes nothing: no log line, and no
    other open form is told it changed (Spec 10.5, 10.7). Rejected: deleting and writing every row
    again, which is no change for the log (`capture` compares child rows) but is one for the live
    feed. The default moves in two steps: the unique index allows one default per part at any time.
    """
    rows = part_suppliers(session, part_id)
    for row in rows[len(wanted):]:
        session.delete(row)
    for row, item in zip(rows, wanted):
        if row.is_default and not item.is_default:
            row.is_default = False
    session.flush()
    for position, item in enumerate(wanted):
        row = rows[position] if position < len(rows) else None
        if row is None:
            row = PartSupplier(part_id=part_id, position=position)
            session.add(row)
        for column in SUPPLIER_COLUMNS:
            if getattr(row, column) != getattr(item, column):
                setattr(row, column, getattr(item, column))
    session.flush()


def part_suppliers(session: Session, part_id: int) -> list[PartSupplier]:
    """The suppliers of a part in the order of its form."""
    return list(
        session.scalars(
            select(PartSupplier).where(PartSupplier.part_id == part_id).order_by(PartSupplier.position)
        )
    )


def default_supplier(session: Session, part_id: int) -> PartSupplier | None:
    """The default supplier of a part (every part has one, Spec 5.3)."""
    return session.scalar(
        select(PartSupplier).where(PartSupplier.part_id == part_id, PartSupplier.is_default.is_(True))
    )


def suppliers_by_part(session: Session) -> dict[int, list[PartSupplier]]:
    """The suppliers of every part, in the order of their forms; one query for a list page."""
    found: dict[int, list[PartSupplier]] = {}
    for row in session.scalars(select(PartSupplier).order_by(PartSupplier.part_id, PartSupplier.position)):
        found.setdefault(row.part_id, []).append(row)
    return found


def supplier_names(session: Session) -> list[str]:
    """The supplier names in use, offered while typing (Spec 5.3); each once, sorted."""
    names = session.scalars(select(PartSupplier.name).where(PartSupplier.name.is_not(None)).distinct())
    return sorted(set(names), key=lambda name: (name.casefold(), name))


def create_part(session: Session, data: PartInput, initial_stock: int, actor: Actor) -> Part:
    """New purchased part; the initial stock is booked as an inventory count (Spec 5.3, 6.5)."""
    data = _check_part_input(data)
    initial_stock = _int_in(initial_stock, "parts.field.initial_stock", 0, MAX_QTY)
    part = Part()
    _apply_part(part, data)
    session.add(part)
    session.flush()
    _apply_suppliers(session, part.id, data.suppliers)
    if initial_stock > 0:
        count_part(session, part.id, initial_stock, actor)
    return part


def update_part(session: Session, part_id: int, data: PartInput) -> Part:
    """Change the settings and suppliers of a part; its stock only changes through bookings."""
    part = session.get(Part, part_id)
    if part is None:
        raise NotFoundError("parts.not_found")
    data = _check_part_input(data)
    _apply_part(part, data)
    session.flush()
    _apply_suppliers(session, part_id, data.suppliers)
    return part


def list_parts(session: Session, *, archived: bool = False) -> list[Part]:
    parts = session.scalars(select(Part).where(_archive_filter(Part.archived_at, archived))).all()
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
    orderable: bool  # every position still has an active article
    status: SetStatus  # deliverable when at least one set comes together (Spec 10.1)


def _check_set_input(
    session: Session, data: SetInput, *, set_id: int | None = None
) -> tuple[str, str | None, list[tuple[int, list[int]]]]:
    name = _required_name(data.name, "sets.field.name")
    sku = validate_sku(session, data.sku, set_id=set_id)
    if not data.positions:
        raise ValidationError("sets.no_positions")
    positions: list[tuple[int, list[int]]] = []
    kept: set[int] = set()  # archived articles that are already part of this set may stay
    if set_id is not None:
        kept = set(
            session.scalars(
                select(SetPositionOption.article_id)
                .join(SetPosition, SetPosition.id == SetPositionOption.position_id)
                .where(SetPosition.set_id == set_id)
            )
        )
    for position in data.positions:
        qty = _int_in(position.qty, "sets.field.qty", 1, 1000)
        article_ids = list(dict.fromkeys(position.article_ids))
        if not article_ids:
            raise ValidationError("sets.position_empty")
        for article_id in article_ids:
            article = session.get(Article, article_id)
            if article is None:
                raise NotFoundError("articles.not_found")
            if article.archived_at is not None and article_id not in kept:
                raise ValidationError("sets.article_archived", name=article.name)
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
        # Options first: they point at their position.
        _delete_rows(session, select(SetPositionOption).where(SetPositionOption.position_id.in_(old)))
        _delete_rows(session, select(SetPosition).where(SetPosition.id.in_(old)))
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
            .where(SetPositionOption.position_id == position.id, Article.archived_at.is_(None))
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
                position.qty,
                tuple(rules.available(a.stock, a.reserved) for a in articles),
                tuple(a.id for a in articles),
            )
        )
    available = rules.set_available(stocks)
    return SetRow(
        id=product_set.id,
        name=product_set.name,
        sku=product_set.sku,
        available=available,
        positions=rows,
        orderable=bool(rows) and all(row.article_ids for row in rows),
        status=set_status(available=available),
    )


def list_sets(session: Session, *, archived: bool = False) -> list[SetRow]:
    found = session.scalars(select(ProductSet).where(_archive_filter(ProductSet.archived_at, archived))).all()
    sets = sorted(found, key=lambda s: (s.name.casefold(), s.id))
    return [_set_row(session, s) for s in sets]


# ---- archive instead of delete (Spec 5.5) ---------------------------------------------------


def _article_or_404(session: Session, article_id: int) -> Article:
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    return article


def archive_article(session: Session, article_id: int) -> None:
    """Only when nothing is running. B3 knows open orders, provisional and awaiting
    assembly; B4 adds the queue and in-progress checks."""
    article = _article_or_404(session, article_id)
    if article.archived_at is not None:
        raise ConflictError("catalog.already_archived")
    in_open_order = session.scalar(
        select(OrderLine.id)
        .join(Order, Order.id == OrderLine.order_id)
        .where(OrderLine.article_id == article_id, Order.status == "open")
        .limit(1)
    )
    if in_open_order is not None:
        raise ConflictError("articles.archive_open_orders")
    if article.provisional > 0:
        raise ConflictError("articles.archive_provisional")
    if article.awaiting_assembly > 0:
        raise ConflictError("articles.archive_awaiting_assembly")
    if session.scalar(
        select(WorkQueueEntry.id).where(WorkQueueEntry.article_id == article_id).limit(1)
    ):
        raise ConflictError("articles.archive_in_queue")
    if session.scalar(
        select(Dispatch.id)
        .where(Dispatch.article_id == article_id, Dispatch.status.in_(OPEN_STATES))
        .limit(1)
    ):
        raise ConflictError("articles.archive_in_progress")
    article.archived_at = utcnow()
    session.flush()


def _plate_holder(session: Session, file_id: int, plate_index: int, *, besides: int) -> Article | None:
    return session.scalar(
        select(Article).where(
            Article.library_file_id == file_id,
            Article.plate_id == plate_index,
            Article.archived_at.is_(None),
            Article.id != besides,
        )
    )


def _check_parts_active(session: Session, article_id: int) -> None:
    archived = session.scalar(
        select(Part)
        .join(ArticlePart, ArticlePart.part_id == Part.id)
        .where(ArticlePart.article_id == article_id, Part.archived_at.is_not(None))
        .limit(1)
    )
    if archived is not None:
        raise ValidationError("parts.archived", name=archived.name)


def restore_article(session: Session, article_id: int) -> Article:
    """Bring an article back. If its plate has been taken meanwhile, the caller offers
    `restore_article_with_plate` with another file."""
    article = _article_or_404(session, article_id)
    if article.archived_at is None:
        raise ConflictError("catalog.not_archived")
    holder = _plate_holder(session, article.library_file_id, article.plate_id, besides=article.id)
    if holder is not None:
        raise ConflictError("articles.restore_plate_taken", name=holder.name)
    _check_parts_active(session, article_id)
    article.archived_at = None
    session.flush()
    return article


def restore_article_with_plate(
    session: Session,
    article_id: int,
    *,
    file: LibraryFile,
    plate: Plate,
    requirements: Sequence[FilamentRequirement],
    thumbnail: bytes | None,
) -> Article:
    """Restore with another file and plate; takes over the file data like `create_article`."""
    article = _article_or_404(session, article_id)
    if article.archived_at is None:
        raise ConflictError("catalog.not_archived")
    holder = _plate_holder(session, file.id, plate.index, besides=article.id)
    if holder is not None:
        raise ConflictError("articles.plate_taken", name=holder.name)
    _check_parts_active(session, article_id)
    article.library_file_id = file.id
    article.plate_id = plate.index
    article.file_name = file.filename
    article.file_fingerprint = file.fingerprint
    article.thumbnail = thumbnail
    _take_over_filaments(session, article_id, requirements)
    article.archived_at = None
    session.flush()
    return article


def _filament_kinds(session: Session, article_id: int) -> dict[int, tuple[str | None, str | None]]:
    """Slot -> (material, colour) of the file the article holds now."""
    return {
        row.slot: (row.material, row.color_hex)
        for row in session.scalars(select(ArticleFilament).where(ArticleFilament.article_id == article_id))
    }


def same_filament_kind(a: tuple[str | None, str | None] | None, b: tuple[str | None, str | None] | None) -> bool:
    """Two slots are of the same kind: the same material and the same colour (`(material, colour)` of each).

    The one comparison of the program for "the same filament in a slot": a slot keeps its filament
    group after another print file is chosen (Spec 5.1), and the filament page of a new article starts
    from the group of a similar article (Spec 5.2, p156; `filament.suggest_from_similar`). `None` is
    "no such slot": it equals only itself."""
    return a == b


def slots_losing_group(session: Session, article_id: int, requirements: Sequence[FilamentRequirement]) -> list[int]:
    """Slots of the new file the person has to tick again after a change of the file (Spec 5.1).

    Empty when no ticked group is lost: a slot that never had a group is no news. When a group is lost
    (the slot is another material or colour now, or the new file has no such slot), every slot of the new
    file without a kept group is named. Read before the change; the same rule as `_take_over_filaments`."""
    old = _filament_kinds(session, article_id)
    ticked = set(
        session.scalars(select(ArticleFilamentSort.slot).where(ArticleFilamentSort.article_id == article_id))
    )
    new = {r.slot: (r.material, r.color_hex) for r in requirements}
    kept = {slot for slot in ticked if same_filament_kind(old.get(slot), new.get(slot))}
    if ticked <= kept:
        return []
    return sorted(slot for slot in new if slot not in kept)


def _take_over_filaments(
    session: Session,
    article_id: int,
    requirements: Sequence[FilamentRequirement],
) -> None:
    """`_replace_filaments`, left out of the activity log: a new print file is the journal's (Spec 10.7)."""
    with quiet(session):
        _replace_filaments(session, article_id, requirements)


def _replace_filaments(
    session: Session,
    article_id: int,
    requirements: Sequence[FilamentRequirement],
) -> None:
    """Replace the filament rows. A slot keeps its filament group (the ticked spool sorts, Spec 5.2)
    when material and colour stay the same (plan B4 decision 10; for every choice of a file, Spec 5.1);
    every other group of the article is dropped, so that a slot never counts spool sorts chosen for
    another colour."""
    old = _filament_kinds(session, article_id)
    new = {r.slot: (r.material, r.color_hex) for r in requirements}
    keep = {slot for slot, value in new.items() if same_filament_kind(old.get(slot), value)}
    _delete_rows(
        session,
        select(ArticleFilamentSort).where(
            ArticleFilamentSort.article_id == article_id, ArticleFilamentSort.slot.not_in(keep)
        ),
    )
    _delete_rows(session, select(ArticleFilament).where(ArticleFilament.article_id == article_id))
    for requirement in requirements:
        session.add(
            ArticleFilament(
                article_id=article_id,
                slot=requirement.slot,
                grams=requirement.grams,
                material=requirement.material,
                color_hex=requirement.color_hex,
            )
        )


def _active_article(session: Session, article_id: int) -> Article:
    article = _article_or_404(session, article_id)
    if article.archived_at is not None:
        raise ConflictError("articles.edit_archived")
    return article


def _lift_file_pause(article: Article, reasons: tuple[str, ...]) -> None:
    if article.paused and article.pause_reason in reasons:
        article.paused = False
        article.pause_reason = None
        article.consecutive_failures = 0


def confirm_article_file(
    session: Session,
    article_id: int,
    *,
    file: LibraryFile,
    requirements: Sequence[FilamentRequirement],
    thumbnail: bytes | None,
    actor: Actor,
) -> Article:
    """Spec 5.1: accept the changed file as it is now; lifts a `file_changed` pause.

    A `file_missing` pause stays (the file is not there); other pauses stay too.
    """
    article = _active_article(session, article_id)
    if file.id != article.library_file_id:
        raise ConflictError("articles.confirm_other_file")
    details = {
        "old_name": article.file_name,
        "old_fingerprint": article.file_fingerprint,
        "new_name": file.filename,
        "new_fingerprint": file.fingerprint,
    }
    article.file_name = file.filename
    article.file_fingerprint = file.fingerprint
    article.thumbnail = thumbnail
    _take_over_filaments(session, article_id, requirements)
    _lift_file_pause(article, ("file_changed",))
    session.flush()
    write_journal(
        session, kind="article_file_confirmed", actor=actor, article_id=article.id, details=details
    )
    return article


def relink_article(
    session: Session,
    article_id: int,
    *,
    file: LibraryFile,
    plate: Plate,
    requirements: Sequence[FilamentRequirement],
    thumbnail: bytes | None,
    actor: Actor,
) -> Article:
    """Spec 5.1: choose another file and plate for an active article; lifts a file pause."""
    article = _active_article(session, article_id)
    holder = _plate_holder(session, file.id, plate.index, besides=article.id)
    if holder is not None:
        raise ConflictError("articles.plate_taken", name=holder.name)
    details = {
        "old_file_id": article.library_file_id,
        "old_plate": article.plate_id,
        "new_file_id": file.id,
        "new_plate": plate.index,
    }
    article.library_file_id = file.id
    article.plate_id = plate.index
    article.file_name = file.filename
    article.file_fingerprint = file.fingerprint
    article.thumbnail = thumbnail
    _take_over_filaments(session, article_id, requirements)
    _lift_file_pause(article, ("file_changed", "file_missing"))
    session.flush()
    write_journal(
        session, kind="article_relinked", actor=actor, article_id=article.id, details=details
    )
    return article


def archive_part(session: Session, part_id: int) -> None:
    part = session.get(Part, part_id)
    if part is None:
        raise NotFoundError("parts.not_found")
    if part.archived_at is not None:
        raise ConflictError("catalog.already_archived")
    user = session.scalar(
        select(Article)
        .join(ArticlePart, ArticlePart.article_id == Article.id)
        .where(ArticlePart.part_id == part_id, Article.archived_at.is_(None))
        .order_by(Article.name)
        .limit(1)
    )
    if user is not None:
        raise ConflictError("parts.archive_in_use", article=user.name)
    part.archived_at = utcnow()
    session.flush()


def restore_part(session: Session, part_id: int) -> Part:
    part = session.get(Part, part_id)
    if part is None:
        raise NotFoundError("parts.not_found")
    if part.archived_at is None:
        raise ConflictError("catalog.not_archived")
    part.archived_at = None
    session.flush()
    return part


def archive_set(session: Session, set_id: int) -> None:
    """A set can always be archived; open orders keep their lines and still ship."""
    product_set = get_set(session, set_id)
    if product_set.archived_at is not None:
        raise ConflictError("catalog.already_archived")
    product_set.archived_at = utcnow()
    session.flush()


def restore_set(session: Session, set_id: int) -> ProductSet:
    product_set = get_set(session, set_id)
    if product_set.archived_at is None:
        raise ConflictError("catalog.not_archived")
    product_set.archived_at = None
    session.flush()
    return product_set
