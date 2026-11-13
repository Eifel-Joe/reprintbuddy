"""Constants, validation helpers, print target and articles of the catalog."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate, Printer
from reprintbuddy.domain import articlesets, inputs, rules, workqueue
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.domain.status import ArticleStatus, article_status
from reprintbuddy.domain.stock import MAX_QTY
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
MAX_LABEL_TEXT = 60  # the text for the label (Spec 5.2): a label has little room


# ---- validation helpers ----------------------------------------------------------------


def _required_name(value: str | None, field_key: str) -> str:
    name = inputs.text(value, field_key, MAX_NAME, required=True)
    assert name is not None
    return name


def same_number(sku: str) -> str:
    """The form in which two article numbers are compared: case does not count, EX-S10-A and ex-s10-a
    are the same number (Spec 5.4 "Artikelnummern", p350). Numbers are stored as typed; only the
    comparison folds. Replaced: the exact comparison, which let a typo in case make a second number.
    Pairs stored before stay; saving one of them is refused until one is renamed.
    Tests: tests/domain/test_sku_case.py."""
    return sku.casefold()


def validate_sku(
    session: Session,
    sku: str | None,
    *,
    article_id: int | None = None,
    set_id: int | None = None,
    variants: dict[str, int] | None = None,
    required: bool = False,
) -> str | None:
    """Trimmed article number, unique over articles, sets and variants (all become SKUs later), case
    not counted (p350). The refusal names the number that is taken, as it is stored.

    `required` (p533, Spec 5.2): an article, a set and an article made of parts are not saved without a
    number, and the refusal says the number is missing. Only a part ("Sold alone: no") may go without.
    What an earlier version stored without a number is not looked at here; it is asked for at the next
    save, which comes through here."""
    # Here, not at the top: sets.py imports this module, a top-level import would be a cycle (0.4.8, R10).
    from reprintbuddy.domain.catalog.sets import variant_numbers

    sku = inputs.text(sku, "catalog.field.sku", MAX_SKU, required=required)
    if sku is None:
        return None
    wanted = same_number(sku)
    stored = [
        *((n, i != article_id) for i, n in session.execute(select(Article.id, Article.sku).where(Article.sku.is_not(None)))),
        *((n, i != set_id) for i, n in session.execute(select(ProductSet.id, ProductSet.sku).where(ProductSet.sku.is_not(None)))),
        # a variant's number (Spec 5.4, p340); a set passes the numbers it already looked up
        *((n, True) for n in (variants if variants is not None else variant_numbers(session, exclude_set_id=set_id))),
    ]
    for number, other in stored:
        if other and same_number(number) == wanted:
            raise ConflictError("catalog.sku_taken", sku=number)
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
    sold_alone: bool = True  # "einzeln erhaeltlich" (Spec 5.2, 0.4.0); off = a part only (Bauteil)


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


def check_purchased_parts(session: Session, wanted: dict[int, int], article_id: int | None) -> dict[int, int]:
    """Purchased parts and their quantity per unit (Spec 5.2); an archived part only when the article
    already uses it. Shared by print articles and articles made of parts (0.4.0)."""
    parts: dict[int, int] = {}
    linked = _linked_parts(session, article_id)
    for part_id, qty in wanted.items():
        parts[part_id] = inputs.int_in(qty, "articles.field.qty_per_unit", 1, 1000)
        part = session.get(Part, part_id)
        if part is None:
            raise NotFoundError("parts.not_found")
        if part.archived_at is not None and part_id not in linked:
            raise ValidationError("parts.archived", name=part.name)
    return parts


def _check_article_input(
    session: Session, data: ArticleInput, *, article_id: int | None = None
) -> ArticleInput:
    name = _required_name(data.name, "articles.field.name")
    # Only a part ("Sold alone: no") may stay without a number; it needs one once it is sold alone (p533).
    sku = validate_sku(session, data.sku, article_id=article_id, required=data.sold_alone)
    label_text = inputs.text(data.label_text, "articles.field.label_text", MAX_LABEL_TEXT)
    target_model = inputs.text(data.target_model, "articles.field.target", MAX_NAME)
    printer_id = data.printer_id
    if printer_id is not None:
        printer_id = inputs.int_in(printer_id, "articles.field.printer_id", 1, MAX_QTY)
    if target_model is not None and printer_id is not None:
        raise ValidationError("articles.target_exclusive")
    # The same rule as the switch of the list (`set_auto_enabled`): the automation needs a print
    # target, `dispatch.reserve_next` would pause the article with `no_target` at once (p52).
    if data.auto_enabled and target_model is None and printer_id is None:
        raise ValidationError("articles.field_auto_needs_target", field="articles.field.auto_enabled")
    parts = check_purchased_parts(session, data.parts, article_id)
    return replace(
        data,
        name=name,
        sku=sku,
        label_text=label_text,
        units_per_print=inputs.int_in(data.units_per_print, "articles.field.units_per_print", 1, 1000),
        min_stock=inputs.int_in(data.min_stock, "articles.field.min_stock", 0, MAX_QTY),
        buffer=inputs.int_in(data.buffer, "articles.field.buffer", 0, MAX_QTY),
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
    article.sold_alone = data.sold_alone


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
            articlesets.where_print(),
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


def sets_using(session: Session, article_id: int) -> list[str]:
    """Names of the active sets an article stands in, by name (0.4.0: what a set may hold, Spec 5.4)."""
    return list(session.scalars(
        select(ProductSet.name)
        .join(SetPosition, SetPosition.set_id == ProductSet.id)
        .join(SetPositionOption, SetPositionOption.position_id == SetPosition.id)
        .where(SetPositionOption.article_id == article_id, ProductSet.archived_at.is_(None))
        .distinct()
        .order_by(ProductSet.name)
    ))


def update_article(session: Session, article_id: int, data: ArticleInput) -> Article:
    """Change the settings of a print article; file and plate stay."""
    article = session.get(Article, article_id)
    if article is None or article.kind != articlesets.PRINT:  # review finding 5: the others have own forms
        raise NotFoundError("articles.not_found")
    if article.archived_at is not None:
        raise ConflictError("articles.edit_archived")
    linked = _linked_parts(session, article_id)
    data = _check_article_input(session, data, article_id=article_id)
    if article.sold_alone and not data.sold_alone:
        sets = sets_using(session, article_id)
        if sets:  # review finding 2: a part only stands in no set (Spec 5.2, 5.4)
            raise ConflictError("articles.sold_alone_in_set", names=", ".join(sets))
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
    target = inputs.int_in(target, "articles.target_quantity", 0, MAX_QTY)
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
    kind: str = "print"  # 0.4.0 (p341): "print" or "assembled"; variant rows are never rows of their own
    sold_alone: bool = True
    demand: int = 0  # Bedarf aus Montage (Spec 7.1): the status counts with minimum + demand
    assembled: object | None = None  # assembled.AssembledRow of an article made of parts


def article_row(
    article: Article, *, queued_units: int = 0, in_progress_units: int = 0, in_progress_prints: int = 0,
    demand: int = 0,
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
        # A part with demand from assembly counts with minimum + demand (Spec 10.1, 7.1; 0.4.0).
        status=article_status(available=available, min_stock=article.min_stock + demand, coverage=coverage),
        queued_units=queued_units,
        in_progress_units=in_progress_units,
        in_progress_prints=in_progress_prints,
        auto_enabled=article.auto_enabled,
        buffer=article.buffer,
        kind=article.kind,
        sold_alone=article.sold_alone,
        demand=demand,
    )


def assembled_article_row(session: Session, article: Article, row) -> ArticleRow:
    """The row of an article made of parts in the list and the overview (Spec 10.1, 0.4.0 p341), from
    its `assembled.AssembledRow`: on stock the sums over its variants, on order "zusammen hoechstens"."""
    on_stock = row.mode == "stock"
    single = row.holders[0] if on_stock and len(row.holders) == 1 and not row.fields else None
    picture = session.get(Article, row.thumbnail_article_id) if row.thumbnail_article_id else None
    return ArticleRow(
        id=article.id,
        name=article.name,
        sku=article.sku,
        stock=sum(h.stock for h in row.holders),
        reserved=sum(h.reserved for h in row.holders),
        available=row.available,
        provisional=0,
        awaiting_assembly=0,
        min_stock=single.min_stock if single else 0,
        target=single.target if single else 0,
        has_thumbnail=bool(picture is not None and picture.thumbnail),
        paused=False,
        coverage=row.available,
        status=row.status,  # type: ignore[arg-type]  # on order: deliverable / not deliverable
        queued_units=0,
        in_progress_units=0,
        in_progress_prints=0,
        auto_enabled=False,
        buffer=single.buffer if single else 0,
        kind=article.kind,
        sold_alone=True,
        demand=0,
        assembled=row,
    )


def _archive_filter(column, archived: bool):
    return column.is_not(None) if archived else column.is_(None)


def list_articles(session: Session, *, archived: bool = False) -> list[ArticleRow]:
    """Active articles; with `archived=True` only the archived ones (Spec 5.5). Print articles and
    articles made of parts, never a variant row: it shows under its article (0.4.0, p341)."""
    from reprintbuddy.domain import assembled  # assembled builds on this module

    articles = session.scalars(articlesets.catalog_articles(archived=archived)).all()
    # one query per table; an archived article has no entry and no open print (plan B4 decision 20)
    coverages = {} if archived else workqueue.coverage_all(session)
    prints = {} if archived else workqueue.in_progress_prints(session)
    demand = {} if archived else assembled.assembly_demand(session)
    rows = []
    for a in articles:
        if a.kind == articlesets.ASSEMBLED:
            row = assembled.assembled_row(session, a.id, demand=demand, coverage=coverages)
            rows.append(assembled_article_row(session, a, row))
            continue
        c = coverages.get(a.id)
        rows.append(
            article_row(
                a,
                queued_units=c.queued_units if c else 0,
                in_progress_units=c.in_progress_units if c else 0,
                in_progress_prints=prints.get(a.id, 0),
                demand=demand.get(a.id, 0),
            )
        )
    return sorted(rows, key=lambda r: (r.name.casefold(), r.id))
