"""Articles made of parts (Spec 5.6, 0.4.0 p341): bill of materials, variant rows, demand, building.

An article made of parts is a row of `articles` with `kind = 'assembled'`. Its bill of materials is
in `article_components` (base parts: field 0; the articles of an option: field 1 or 2 with the
option) and `article_options` (the options of the two choice fields with their suffixes); its
purchased parts stay in `article_parts`. With choices, each variant is an article row of its own
(`kind = 'variant'`, `parent_id`, `variant_key` = the option ids), which holds the stock when the
article is built on stock and only the number when it is built on order. Without choices the article
itself is its only variant. Stock bookings stay in `domain/stock.py`.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate
from reprintbuddy.domain import articlesets, catalog, rules, stock, workqueue
from reprintbuddy.domain.catalog import BaseInput
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.domain.status import assembled_status, set_status
from reprintbuddy.domain.stock import MAX_QTY, Actor, PartShortage
from reprintbuddy.domain.workqueue import Coverage
from reprintbuddy.models import (
    Article,
    ArticleComponent,
    ArticleFilament,
    ArticleFilamentSort,
    ArticleOption,
    ArticlePart,
    Order,
    OrderLine,
    Part,
    utcnow,
)

log = logging.getLogger(__name__)

Mode = Literal["stock", "order"]
MODES = ("stock", "order")
MAX_ROW_QTY = 1000  # quantity per row of a bill of materials, as in sets (Spec 5.4)
MIN_UNITS = 2  # a variant holds at least two units of parts and purchased parts (Spec 5.6 "Umfang")


@dataclass
class OptionInput:
    option_id: int | None  # an existing option keeps its variants (and their stock); None = new
    suffix: str | None
    rows: list[BaseInput]  # (qty, print article) per row


@dataclass
class AssembledInput:
    name: str
    sku: str | None
    mode: Mode
    base: list[BaseInput]
    fields: list[list[OptionInput]]  # choice fields 1 and 2; an empty last field is no field
    parts: dict[int, int]  # purchased part id -> quantity per unit
    label_text: str | None = None
    min_stock: int = 0  # without choices, built on stock
    buffer: int = 0
    variant_targets: dict[int, tuple[int, int]] = field(default_factory=dict)  # variant row id -> (min, buffer)


@dataclass(frozen=True)
class _Checked:
    name: str
    sku: str | None
    mode: Mode
    label_text: str | None
    min_stock: int
    buffer: int
    base: list[tuple[int, int]]  # (part id, qty)
    fields: list[list[tuple[int | None, str, list[tuple[int, int]]]]]  # (option id, suffix, rows)
    parts: dict[int, int]


# ---- reading the bill of materials -----------------------------------------------------------


def options(session: Session, composite: Article) -> list[list[ArticleOption]]:
    """The options of choice field 1 and 2, in the order of the form; no list for an unused field."""
    rows = session.scalars(
        select(ArticleOption)
        .where(ArticleOption.article_id == composite.id)
        .order_by(ArticleOption.field, ArticleOption.sort, ArticleOption.id)
    ).all()
    fields: list[list[ArticleOption]] = []
    for number in (1, 2):
        found = [o for o in rows if o.field == number]
        if found:
            fields.append(found)
    return fields


def components(session: Session, composite: Article) -> list[ArticleComponent]:
    return list(
        session.scalars(
            select(ArticleComponent)
            .where(ArticleComponent.article_id == composite.id)
            .order_by(ArticleComponent.field, ArticleComponent.option_id, ArticleComponent.sort, ArticleComponent.id)
        )
    )


def base_needs(session: Session, composite: Article) -> dict[int, int]:
    return {c.part_id: c.qty for c in components(session, composite) if c.field == 0}


def option_needs(session: Session, composite: Article) -> dict[int, dict[int, int]]:
    """Option id -> (part id -> qty)."""
    needs: dict[int, dict[int, int]] = {}
    for c in components(session, composite):
        if c.option_id is not None:
            needs.setdefault(c.option_id, {})[c.part_id] = c.qty
    return needs


def _key(combination) -> str:
    return ":".join(str(o.id) for o in combination)


def parent_of(session: Session, row: Article) -> Article:
    """The article made of parts of a variant row, or the row itself."""
    if row.kind == articlesets.VARIANT:
        parent = session.get(Article, row.parent_id)
        assert parent is not None
        return parent
    return row


def needs_of_key(session: Session, composite: Article, key: str) -> dict[int, int]:
    """Part id -> quantity per piece for one variant: the base parts and the chosen options."""
    needs = dict(base_needs(session, composite))
    if key:
        per_option = option_needs(session, composite)
        for option_id in key.split(":"):
            for part_id, qty in per_option.get(int(option_id), {}).items():
                needs[part_id] = needs.get(part_id, 0) + qty
    return needs


def needs_of(session: Session, holder: Article) -> dict[int, int]:
    """What one piece of a variant row (or an article made of parts without choices) takes."""
    composite = parent_of(session, holder)
    return needs_of_key(session, composite, holder.variant_key or "")


def variants(session: Session, composite: Article) -> list[Article]:
    """The active variants in the order of the form: the variant rows, or the article itself when it
    has no choices."""
    fields = options(session, composite)
    if not fields:
        return [composite]
    rows = {r.variant_key: r for r in session.scalars(articlesets.variant_rows(composite.id))}
    return [rows[_key(c)] for c in itertools.product(*fields) if _key(c) in rows]


def holders(session: Session, composite: Article) -> list[Article]:
    """The rows that hold finished stock (Spec 5.6): the variants when built on stock, none on order."""
    if composite.assembly_mode != "stock":
        return []
    return variants(session, composite)


# ---- checking the input ------------------------------------------------------------------------


def _bom_articles(session: Session, composite_id: int | None) -> set[int]:
    if composite_id is None:
        return set()
    return set(session.scalars(select(ArticleComponent.part_id).where(ArticleComponent.article_id == composite_id)))


def _check(session: Session, data: AssembledInput, *, article_id: int | None = None) -> _Checked:
    if data.mode not in MODES:
        raise ValueError(f"unknown assembly mode {data.mode!r}")
    name = catalog._required_name(data.name, "articles.field.name")
    sku = catalog.validate_sku(session, data.sku, article_id=article_id)
    label_text = catalog._text(data.label_text, "articles.field.label_text", catalog.MAX_LABEL_TEXT)
    min_stock = catalog._int_in(data.min_stock, "articles.field.min_stock", 0, MAX_QTY)
    buffer = catalog._int_in(data.buffer, "articles.field.buffer", 0, MAX_QTY)
    purchased = catalog.check_purchased_parts(session, data.parts, article_id)
    kept = _bom_articles(session, article_id)  # an archived part already in the list may stay

    def part(article_id_: int) -> Article:
        found = session.get(Article, article_id_)
        if found is None:
            raise NotFoundError("articles.not_found")
        if found.kind != articlesets.PRINT:
            raise ValidationError("assembled.part_not_print", name=found.name)
        if found.archived_at is not None and article_id_ not in kept:
            raise ValidationError("assembled.part_archived", name=found.name)
        return found

    base: list[tuple[int, int]] = []
    in_base: set[int] = set()
    for row in data.base:
        found = part(row.article_id)
        if found.id in in_base:
            raise ValidationError("assembled.article_twice", name=found.name)
        in_base.add(found.id)
        base.append((found.id, catalog._int_in(row.qty, "assembled.field.qty", 1, MAX_ROW_QTY)))

    fields_in = list(data.fields)
    while fields_in and not fields_in[-1]:
        fields_in.pop()  # an empty last field is no field
    if len(fields_in) > catalog.CHOICE_FIELDS:
        raise ValueError("an article made of parts has at most two choice fields")
    if any(not f for f in fields_in):
        raise ValidationError("assembled.choices2_without_1")
    fields: list[list[tuple[int | None, str, list[tuple[int, int]]]]] = []
    seen_in_fields: dict[int, int] = {}  # article id -> field number
    for number, options_in in enumerate(fields_in, start=1):
        suffixes: set[str] = set()
        checked_options = []
        for option in options_in:
            if not option.rows:
                raise ValidationError("assembled.option_empty", suffix=(option.suffix or "").strip())
            rows: list[tuple[int, int]] = []
            in_option: set[int] = set()
            for row in option.rows:
                found = part(row.article_id)
                if found.id in in_option or found.id in in_base or seen_in_fields.get(found.id, number) != number:
                    raise ValidationError("assembled.article_twice", name=found.name)
                in_option.add(found.id)
                seen_in_fields[found.id] = number
                rows.append((found.id, catalog._int_in(row.qty, "assembled.field.qty", 1, MAX_ROW_QTY)))
            suffix = catalog._text(option.suffix, "sets.field.suffix", catalog.MAX_SKU)
            if suffix is None:
                raise ValidationError("assembled.suffix_required", name=session.get(Article, rows[0][0]).name)
            if suffix in suffixes:
                raise ValidationError("assembled.suffix_twice", suffix=suffix)
            suffixes.add(suffix)
            checked_options.append((option.option_id, suffix, rows))
        fields.append(checked_options)

    purchased_units = sum(purchased.values())
    base_units = sum(q for _a, q in base)
    for combination in itertools.product(*fields) if fields else [()]:
        part_units = base_units + sum(q for _id, _suffix, rows in combination for _a, q in rows)
        if part_units < 1 or part_units + purchased_units < MIN_UNITS:
            raise ValidationError("assembled.too_small")
    if fields and sku is None:
        raise ValidationError("assembled.sku_required")
    if data.mode == "order" or fields:
        min_stock = buffer = 0
    return _Checked(name, sku, data.mode, label_text, min_stock, buffer, base, fields, purchased)


# ---- variant rows ------------------------------------------------------------------------------


def _all_variant_rows(session: Session, composite_id: int) -> list[Article]:
    return list(
        session.scalars(
            select(Article).where(Article.kind == articlesets.VARIANT, Article.parent_id == composite_id).order_by(Article.id)
        )
    )


def _open_order_lines(session: Session):
    return select(OrderLine.id).join(Order, Order.id == OrderLine.order_id).where(Order.status == "open")


def in_use(session: Session, composite: Article, row: Article) -> bool:
    """A variant has stock, a reservation or stands in an open order (Spec 5.6): it cannot go."""
    if row.stock or row.reserved:
        return True
    if session.scalar(_open_order_lines(session).where(OrderLine.article_id == row.id).limit(1)) is not None:
        return True
    if row.sku is not None and session.scalar(
        _open_order_lines(session)
        .where(OrderLine.assembled_id == composite.id, OrderLine.variant_sku == row.sku)
        .limit(1)
    ) is not None:
        return True
    return False


@dataclass(frozen=True)
class _Plan:
    """What `sync_variants` will do: key -> (number, row to keep or revive or None for a new one)."""

    wanted: dict[str, tuple[str | None, Article | None]]
    dropped: list[Article]


def _plan(session: Session, composite: Article, sku: str | None, fields: list[list[tuple[str, str]]]) -> _Plan:
    """`fields`: per field (key part, suffix) in form order; keys are option ids (as text) or, for an
    option not saved yet, a placeholder that never matches a row."""
    rows = _all_variant_rows(session, composite.id)
    active = {r.variant_key: r for r in rows if r.archived_at is None}
    archived = [r for r in rows if r.archived_at is not None]
    wanted: dict[str, tuple[str | None, Article | None]] = {}
    if fields:
        for combination in itertools.product(*fields):
            key = ":".join(k for k, _s in combination)
            wanted[key] = (rules.variant_number(sku, [s for _k, s in combination]), active.get(key))
    taken_by_revive: set[int] = set()
    for key, (number, row) in list(wanted.items()):
        if row is not None:
            continue
        # The same variant back (archived together with its article and restored), or a number of
        # this article that comes back: revive that row instead of locking the number for good.
        revived = next((r for r in archived if r.variant_key == key and r.id not in taken_by_revive), None)
        if revived is None and number is not None:
            revived = next((r for r in archived if r.sku == number and r.id not in taken_by_revive), None)
        if revived is not None:
            taken_by_revive.add(revived.id)
            wanted[key] = (number, revived)
    dropped = [r for key, r in active.items() if key not in wanted]
    return _Plan(wanted, dropped)


def _check_numbers(session: Session, composite_id: int | None, plan: _Plan) -> None:
    own = {r.id for _n, r in plan.wanted.values() if r is not None}
    by_number = {}
    if composite_id is not None:
        by_number = {r.sku: r.id for r in _all_variant_rows(session, composite_id) if r.sku is not None and r.id in own}
    seen: set[str] = set()
    taken = catalog.variant_numbers(session)
    for number, _row in plan.wanted.values():
        assert number is not None  # base number and suffixes are checked before
        if len(number) > catalog.MAX_SKU:
            raise ValidationError("assembled.variant_too_long", sku=number, max=catalog.MAX_SKU)
        if number in seen:
            raise ConflictError("catalog.sku_taken", sku=number)
        seen.add(number)
        catalog.validate_sku(session, number, article_id=by_number.get(number), variants=taken)


def _check_drops(session: Session, composite: Article, plan: _Plan) -> None:
    for row in plan.dropped:
        if in_use(session, composite, row):
            raise ConflictError("assembled.variant_in_use", sku=row.sku or row.name)


def sync_variants(session: Session, composite: Article, targets: dict[int, tuple[int, int]] | None = None) -> None:
    """Make the variant rows match the options: rename, archive the ones whose option went, revive or
    create the others (Spec 5.6). Checks (numbers, rows in use) are the caller's, before anything
    is written. `targets`: variant row id -> (minimum stock, buffer), on stock only."""
    fields = options(session, composite)
    plan = _plan(session, composite, composite.sku, [[(str(o.id), o.suffix) for o in f] for f in fields])
    now = utcnow()
    for row in plan.dropped:
        row.archived_at = now
    # Numbers may move between rows (suffixes swapped): clear them first, the column is unique.
    moving = [r for number, r in plan.wanted.values() if r is not None and r.sku != number]
    for row in moving:
        row.sku = None
    session.flush()
    on_stock = composite.assembly_mode == "stock"
    for key, (number, row) in plan.wanted.items():
        if row is None:
            row = Article(kind=articlesets.VARIANT, parent_id=composite.id, variant_key=key, name=composite.name)
            session.add(row)
        row.archived_at = None
        row.variant_key = key
        row.sku = number
        row.name = composite.name
        min_stock, buffer = (targets or {}).get(row.id, (row.min_stock, row.buffer)) if row.id else (0, 0)
        row.min_stock, row.buffer = (min_stock, buffer) if on_stock else (0, 0)
    session.flush()


# ---- create and update -------------------------------------------------------------------------


def _write_bom(session: Session, composite: Article, data: _Checked) -> None:
    for row in components(session, composite):
        session.delete(row)
    session.flush()
    existing = {o.id: o for f in options(session, composite) for o in f}
    keep: set[int] = set()
    for number, options_in in enumerate(data.fields, start=1):
        for sort, (option_id, suffix, rows) in enumerate(options_in):
            option = existing.get(option_id) if option_id is not None else None
            if option is None or option.field != number or option.id in keep:
                option = ArticleOption(article_id=composite.id, field=number, sort=sort, suffix=suffix)
                session.add(option)
                session.flush()
            option.sort, option.suffix = sort, suffix
            keep.add(option.id)
            for row_sort, (part_id, qty) in enumerate(rows):
                session.add(ArticleComponent(
                    article_id=composite.id, field=number, option_id=option.id, part_id=part_id, qty=qty, sort=row_sort,
                ))
    for option_id, option in existing.items():
        if option_id not in keep:
            session.delete(option)
    for sort, (part_id, qty) in enumerate(data.base):
        session.add(ArticleComponent(article_id=composite.id, field=0, part_id=part_id, qty=qty, sort=sort))
    session.flush()


def _planned_fields(composite: Article | None, data: _Checked) -> list[list[tuple[str, str]]]:
    """Keys as `sync_variants` will see them: the id of an option that stays, a placeholder for a new one."""
    result = []
    for number, options_in in enumerate(data.fields, start=1):
        result.append([
            (str(option_id) if option_id is not None and composite is not None else f"new-{number}-{i}", suffix)
            for i, (option_id, suffix, _rows) in enumerate(options_in)
        ])
    return result


def _apply(composite: Article, data: _Checked) -> None:
    composite.name = data.name
    composite.sku = data.sku
    composite.label_text = data.label_text
    composite.assembly_mode = data.mode
    composite.min_stock = data.min_stock
    composite.buffer = data.buffer


def create_assembled(session: Session, data: AssembledInput) -> Article:
    """Spec 5.6: a new article made of parts. Creating is no booking."""
    checked = _check(session, data)
    _check_numbers(session, None, _plan_for_new(checked))
    composite = Article(kind=articlesets.ASSEMBLED, sold_alone=True)
    _apply(composite, checked)
    session.add(composite)
    session.flush()
    catalog._set_parts(session, composite.id, checked.parts)
    _write_bom(session, composite, checked)
    sync_variants(session, composite)
    return composite


def _plan_for_new(data: _Checked) -> _Plan:
    wanted: dict[str, tuple[str | None, Article | None]] = {}
    fields = _planned_fields(None, data)
    if fields:
        for combination in itertools.product(*fields):
            wanted[":".join(k for k, _s in combination)] = (rules.variant_number(data.sku, [s for _k, s in combination]), None)
    return _Plan(wanted, [])


def get_assembled(session: Session, article_id: int) -> Article:
    composite = session.get(Article, article_id)
    if composite is None or composite.kind != articlesets.ASSEMBLED:
        raise NotFoundError("articles.not_found")
    return composite


def update_assembled(session: Session, article_id: int, data: AssembledInput) -> Article:
    """Change an article made of parts (Spec 5.6). Stock stays with its variant; a variant with stock,
    a reservation or an open order cannot go; the mode changes only while nothing runs."""
    composite = get_assembled(session, article_id)
    if composite.archived_at is not None:
        raise ConflictError("articles.edit_archived")
    checked = _check(session, data, article_id=article_id)
    own_options = {o.id for f in options(session, composite) for o in f}
    checked = _Checked(**{
        **checked.__dict__,
        "fields": [[(oid if oid in own_options else None, s, r) for oid, s, r in f] for f in checked.fields],
    })
    plan = _plan(session, composite, checked.sku, _planned_fields(composite, checked))
    _check_numbers(session, composite.id, plan)
    if checked.mode != composite.assembly_mode:
        current = [composite, *_all_variant_rows(session, composite.id)]
        if any(r.archived_at is None and in_use(session, composite, r) for r in current):
            raise ConflictError("assembled.mode_switch_busy", name=composite.name)
    _check_drops(session, composite, plan)
    if checked.fields and not options(session, composite) and in_use(session, composite, composite):
        # The article itself was its only variant; with choices it holds no stock any more.
        raise ConflictError("assembled.variant_in_use", sku=composite.sku or composite.name)
    _apply(composite, checked)
    catalog._set_parts(session, composite.id, checked.parts)
    _write_bom(session, composite, checked)
    sync_variants(session, composite, data.variant_targets)
    return composite


# ---- demand from assembly (Spec 7.1) -----------------------------------------------------------


def missing_of(holder: Article) -> int:
    """Fehlmenge of a stock holder: target - available once available is below the minimum, else 0
    (Spec 7.1, as rule 7.1 itself: nothing below the threshold, then up to the target)."""
    available = rules.available(holder.stock, holder.reserved)
    if available >= holder.min_stock:
        return 0
    return rules.target_quantity(holder.min_stock, holder.buffer) - available


def assembly_demand(session: Session) -> dict[int, int]:
    """Bedarf aus Montage per print article: the sum of Fehlmenge x quantity over every variant on
    stock it is in (Spec 7.1, 0.4.0 p341). Only active articles made of parts built on stock count;
    one built on order makes no demand, its orders reserve the parts instead."""
    demand: dict[int, int] = {}
    for composite in session.scalars(
        articlesets.assembled_articles().where(Article.assembly_mode == "stock").order_by(Article.id)
    ):
        for holder in holders(session, composite):
            missing = missing_of(holder)
            if missing <= 0:
                continue
            for part_id, qty in needs_of(session, holder).items():
                demand[part_id] = demand.get(part_id, 0) + missing * qty
    return demand


# ---- rows, availability and status (Spec 5.6, 10.1) --------------------------------------------


@dataclass(frozen=True)
class PartLine:
    article_id: int
    name: str
    qty: int
    available: int
    sold_alone: bool


@dataclass(frozen=True)
class OptionLine:
    option_id: int
    suffix: str
    rows: list[PartLine]


@dataclass(frozen=True)
class HolderRow:
    """One variant (or the article itself without choices)."""

    article_id: int
    key: str
    sku: str | None
    names: list[str]  # the articles of the chosen options
    stock: int
    reserved: int
    available: int
    buildable: int  # how often it can be built from the available stock of its parts
    min_stock: int
    buffer: int
    target: int
    missing: int
    status: str  # ArticleStatus on stock, SetStatus on order


@dataclass(frozen=True)
class AssembledRow:
    id: int
    name: str
    sku: str | None
    mode: str
    base: list[PartLine]
    fields: list[list[OptionLine]]
    purchased: list[tuple[int, str, int]]  # (part id, name, qty per unit)
    holders: list[HolderRow]
    available: int  # on stock: the sum over the variants; on order: "zusammen hoechstens"
    buildable: int  # "zusammen hoechstens" from the parts
    capped: bool  # the search for "zusammen hoechstens" hit its limit: a lower bound
    status: str
    thumbnail_article_id: int | None
    archived: bool


_WORST = ("overbooked", "below_min", "low", "ok")

# Ids of articles made of parts whose "zusammen hoechstens" was capped and already logged (condition 4
# of the plan's approval): one warning while it stays capped, again only after an uncapped result.
_capped_logged: set[int] = set()


def _log_capped(composite: Article, capped: bool) -> None:
    if capped and composite.id not in _capped_logged:
        _capped_logged.add(composite.id)
        log.warning(
            "availability of assembled article %s (%s) hit the search limit of %d nodes;"
            " together-at-most shows a lower bound",
            composite.id, composite.name, rules.MAX_NODES,
        )
    elif not capped:
        _capped_logged.discard(composite.id)


def _covered(part: Article, coverage: Mapping[int, Coverage], demand: Mapping[int, int]) -> bool:
    """Rule 7.1 covers the part: its coverage reaches its minimum plus the demand from assembly."""
    c = coverage.get(part.id)
    total = c.total if c is not None else rules.available(part.stock, part.reserved)
    return total >= part.min_stock + demand.get(part.id, 0)


def assembled_row(
    session: Session,
    article_id: int,
    *,
    demand: Mapping[int, int] | None = None,
    coverage: Mapping[int, Coverage] | None = None,
) -> AssembledRow:
    """Everything a page shows of an article made of parts. A caller with many rows passes `demand`
    (`assembly_demand`) and `coverage` (`workqueue.coverage_all`), computed once."""
    composite = session.get(Article, article_id)
    if composite is None or composite.kind != articlesets.ASSEMBLED:
        raise NotFoundError("articles.not_found")
    if demand is None:
        demand = assembly_demand(session)
    if coverage is None:
        coverage = workqueue.coverage_all(session)
    comps = components(session, composite)
    part_ids = {c.part_id for c in comps}
    parts = {a.id: a for a in session.scalars(select(Article).where(Article.id.in_(part_ids)))} if part_ids else {}
    available = {a.id: rules.available(a.stock, a.reserved) for a in parts.values()}

    def line(c: ArticleComponent) -> PartLine:
        part = parts[c.part_id]
        return PartLine(part.id, part.name, c.qty, available[part.id], part.sold_alone)

    base = [line(c) for c in comps if c.field == 0]
    option_rows = options(session, composite)
    by_option: dict[int, list[PartLine]] = {}
    for c in comps:
        if c.option_id is not None:
            by_option.setdefault(c.option_id, []).append(line(c))
    fields = [[OptionLine(o.id, o.suffix, by_option.get(o.id, [])) for o in f] for f in option_rows]
    bundles = [
        [rules.Bundle(o.id, tuple((p.article_id, p.qty) for p in by_option.get(o.id, []))) for o in f]
        for f in option_rows
    ]
    together = rules.bundle_availability([(p.article_id, p.qty) for p in base], bundles, available)
    _log_capped(composite, together.capped)

    on_stock = composite.assembly_mode == "stock"
    holder_rows: list[HolderRow] = []
    for row in variants(session, composite):
        key = row.variant_key or ""
        option_ids = tuple(int(k) for k in key.split(":")) if key else ()
        buildable = together.variants.get(option_ids, 0) if option_ids else together.total
        names = [p.name for oid in option_ids for p in by_option.get(oid, [])]
        if on_stock:
            avail = rules.available(row.stock, row.reserved)
            covered = all(_covered(parts[p], coverage, demand) for p in needs_of_key(session, composite, key))
            status: str = assembled_status(available=avail, min_stock=row.min_stock, parts_covered=covered)
            missing = missing_of(row)
        else:
            avail, missing = buildable, 0
            status = set_status(available=buildable)
        holder_rows.append(HolderRow(
            article_id=row.id, key=key, sku=row.sku, names=names,
            stock=row.stock if on_stock else 0, reserved=row.reserved if on_stock else 0,
            available=avail, buildable=buildable, min_stock=row.min_stock, buffer=row.buffer,
            target=rules.target_quantity(row.min_stock, row.buffer), missing=missing, status=status,
        ))
    if on_stock:
        total = sum(h.available for h in holder_rows)
        overall = min((h.status for h in holder_rows), key=_WORST.index, default="ok")
    else:
        total = together.total
        overall = set_status(available=total)
    first_option = fields[0][0].rows if fields and fields[0] else []
    first = base[0].article_id if base else (first_option[0].article_id if first_option else None)
    purchased = [
        (part.id, part.name, link.qty_per_unit)
        for link, part in session.execute(
            select(ArticlePart, Part)
            .join(Part, Part.id == ArticlePart.part_id)
            .where(ArticlePart.article_id == composite.id)
            .order_by(Part.name, Part.id)
        ).all()
    ]
    return AssembledRow(
        id=composite.id, name=composite.name, sku=composite.sku, mode=composite.assembly_mode or "stock",
        base=base, fields=fields, purchased=purchased, holders=holder_rows, available=total,
        buildable=together.total, capped=together.capped, status=overall, thumbnail_article_id=first,
        archived=composite.archived_at is not None,
    )


def used_in(session: Session, article_id: int) -> list[tuple[int, str]]:
    """The active articles made of parts a print article is in, by name (Spec 10.3)."""
    rows = session.execute(
        select(Article.id, Article.name)
        .where(
            articlesets.where_assembled(),
            Article.id.in_(select(ArticleComponent.article_id).where(ArticleComponent.part_id == article_id)),
        )
        .order_by(Article.name, Article.id)
    ).all()
    return [(i, n) for i, n in rows]
