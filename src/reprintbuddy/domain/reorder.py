"""Planned demand (Spec 9.2), reorder suggestions and "ordered" marks (Spec 9.3, 9.4).

Functions never commit; the caller owns the transaction. The demand functions only read.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy.activity.capture import acting, note_withdrawn
from reprintbuddy.bambuddy.types import Spool
from reprintbuddy.domain import catalog, filament, rules, suppliers, workqueue
from reprintbuddy.domain import articlesets
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.localtime import local_date
from reprintbuddy.models import (
    naive_utc,
    Article,
    ArticleFilament,
    ArticlePart,
    FilamentSettings,
    FilamentSnapshot,
    FilamentSort,
    FilamentSupplier,
    JournalEntry,
    Order,
    OrderLine,
    Part,
    PartSupplier,
    ReorderMark,
    utcnow,
)

WINDOW_DAYS = 30
MAX_PACKS = 1000


def missing_to_target(article: Article, coverage_total: int, demand: int = 0) -> int:
    """Units still missing up to the target quantity, with the demand from assembly on top (Spec 9.2,
    7.1); never negative."""
    return max(0, rules.target_quantity(article.min_stock, article.buffer) + demand - coverage_total)


def _missing_by_article(session: Session) -> tuple[dict[int, Article], dict[int, int]]:
    """Print articles and what each still misses up to its target (with the demand from assembly)."""
    from reprintbuddy.domain import assembled  # assembled builds on workqueue and catalog

    articles = {
        a.id: a for a in session.scalars(articlesets.print_articles())
    }
    coverage = workqueue.coverage_all(session)
    demand = assembled.assembly_demand(session)
    return articles, {i: missing_to_target(a, coverage[i].total, demand.get(i, 0)) for i, a in articles.items()}


def _assembled_units(session: Session) -> dict[int, int]:
    """Units of each active article made of parts its purchased parts are still needed for (Spec 9.2,
    0.4.0): on stock the units its variants miss up to their target, on order the units ordered and
    not shipped yet."""
    from reprintbuddy.domain import assembled

    units: dict[int, int] = defaultdict(int)
    for composite in session.scalars(articlesets.assembled_articles()):
        if composite.assembly_mode == "stock":
            for holder in assembled.holders(session, composite):
                gap = rules.target_quantity(holder.min_stock, holder.buffer) - rules.available(holder.stock, holder.reserved)
                units[composite.id] += max(0, gap)
    groups = session.execute(
        select(OrderLine.order_id, OrderLine.group_no, OrderLine.assembled_id, OrderLine.set_qty)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.status == "open", OrderLine.assembled_id.is_not(None))
        .distinct()
    ).all()
    for _order_id, _group_no, assembled_id, qty in groups:
        units[assembled_id] += qty or 0
    return dict(units)


def filament_demand(session: Session) -> dict[int, float]:
    """Grams per reordered spool sort: over the slots where it is reordered, grams x (queued +
    in-progress prints + prints up to the target) (Spec 9.2).

    Counts every non-archived article (also with automation off); slots without a reordered sort
    are skipped; sorts without demand may be missing from the result.
    """
    articles, missing = _missing_by_article(session)
    queued = workqueue.queued_prints(session)
    running = workqueue.in_progress_prints(session)
    grams = {(r.article_id, r.slot): r.grams for r in session.scalars(select(ArticleFilament))}
    demand: dict[int, float] = defaultdict(float)
    for group in filament.reorder_groups(session).values():
        for article_id, slot in group.slots:
            article = articles.get(article_id)
            if article is None or (article_id, slot) not in grams:
                continue
            prints = (
                queued.get(article_id, 0)
                + running.get(article_id, 0)
                + rules.prints_for_units(missing[article_id], article.units_per_print)
            )
            demand[group.sort_id] += grams[(article_id, slot)] * prints
    return dict(demand)


def part_demand(session: Session) -> dict[int, int]:
    """Units per purchased part: qty_per_unit x (awaiting assembly + queued units + units in
    progress + units up to the target). "Vorläufig" is deliberately not a summand (Spec 9.2)."""
    articles, missing = _missing_by_article(session)
    queued = workqueue.queued_prints(session)
    units = workqueue.in_progress_units(session)
    assembled_units = _assembled_units(session)
    demand: dict[int, int] = defaultdict(int)
    for link in session.scalars(select(ArticlePart)):
        if link.article_id in assembled_units:
            demand[link.part_id] += link.qty_per_unit * assembled_units[link.article_id]
            continue
        article = articles.get(link.article_id)
        if article is None:
            continue
        per_unit = (
            article.awaiting_assembly
            + queued.get(article.id, 0) * article.units_per_print
            + units.get(article.id, 0)
            + missing[article.id]
        )
        demand[link.part_id] += link.qty_per_unit * per_unit
    return dict(demand)


# ---- consumption of a purchased part (Entscheidung 8) -----------------------------------------


def part_consumption(session: Session, part_id: int, today: date) -> float:
    """Units per day from the bookings of the last 30 days that took the part off (Spec 9.2, 6.6).

    Divided by the days since the oldest row of the whole journal, at most 30 and at least 1;
    0.0 for an empty journal. Every `part_consumed` row counts, whatever booking wrote it:
    assembly, "inspection good" of an article without hand work, stock in without a print with
    "deduct parts" ticked (p174: before, only assembly counted, so the parts of articles without
    hand work had a usage of 0 and their suggestions came too late). Rows count by their booking
    date, not by when they were saved (p172).
    """
    oldest = session.scalar(select(func.min(JournalEntry.booked_at)))
    if oldest is None:
        return 0.0
    since = datetime.combine(today - timedelta(days=WINDOW_DAYS), datetime.min.time())
    rows = session.scalars(
        select(JournalEntry).where(
            JournalEntry.kind == "part_consumed",
            JournalEntry.part_id == part_id,
            JournalEntry.booked_at >= since,
        )
    )
    used = sum(r.qty for r in rows)
    days = max(1, min(WINDOW_DAYS, (today - oldest.date()).days))
    return used / days


# ---- suggestions (Spec 9.3) -------------------------------------------------------------------


@dataclass(frozen=True)
class SupplierOption:
    """What one supplier of a part would deliver for a suggestion (Spec 9.3): the same rule with
    its own lead time, pack size and minimum order."""

    supplier_id: int
    position: int  # in the part's form; "Supplier n" is position + 1
    name: str | None
    order_url: str | None
    packs: int
    pack_size: int
    lead_days: int
    is_default: bool


@dataclass(frozen=True)
class Suggestion:
    item_type: Literal["filament", "part"]
    item_id: int
    label: str
    unit: str
    stock: float
    demand: float
    per_day: float
    lead_days: int
    safety: float
    packs: int
    pack_size: float
    supplier: str | None
    order_url: str | None
    has_history: bool = True  # False: no usage recorded yet, the tile says so instead of "0.0 g/day" (p59)
    on_way: float = 0.0  # ordered and not arrived yet (Spec 9.4, p171); counts like stock, shown apart
    # Every supplier with its own quantity, the default first (Spec 9.3); the fields above (packs,
    # pack size, lead time, supplier, link) are the default's.
    options: tuple[SupplierOption, ...] = ()


def ordered_amount(session: Session, mark: ReorderMark) -> float:
    """The amount a mark ordered, in the item's unit: packs x the pack size (Spec 9.4).

    It counts with the pack size kept at the mark: the supplier's as it was when marked, so a later
    change or removal of the supplier changes nothing (Spec 9.4; parts since 0015, filament since
    0016, which filled the older marks). A mark without one falls back to the default supplier, a
    filament mark without settings to the default unit, as "on its way" shows it (open_marks).
    """
    size = mark.pack_size
    if size is None:
        default = _default_supplier(session, mark.item_type, mark.item_id)
        if default is not None:
            size = default.pack_size
        else:
            size = filament.DEFAULT_ROLL_GRAMS if mark.item_type == "filament" else 0
    return mark.qty * size


def _default_supplier(session: Session, item_type: str, item_id: int) -> PartSupplier | FilamentSupplier | None:
    if item_type == "part":
        return catalog.default_supplier(session, item_id)
    return filament.default_supplier(session, item_id)


def _suppliers_of(session: Session, item_type: str, item_id: int) -> list[PartSupplier] | list[FilamentSupplier]:
    if item_type == "part":
        return catalog.part_suppliers(session, item_id)
    return filament.sort_suppliers(session, item_id)


def suppliers_key(session: Session, item_type: str, item_id: int) -> str:
    """The fingerprint of an item's suppliers as the reorder page shows them (`suppliers.key`)."""
    return suppliers.key(_suppliers_of(session, item_type, item_id))


@dataclass(frozen=True)
class _NewSpool:
    sort_id: int | None
    label_g: float
    created_at: datetime | None


def _spools_seen(session: Session, spools: Sequence[Spool] | None) -> list[_NewSpool]:
    """The non-archived spools Reprintbuddy knows: Bambuddy's list, without it the latest record.

    Filament is never booked in Reprintbuddy; Bambuddy keeps it, Reprintbuddy only reads the spool
    list (Spec 9.1, 9.4). The record keeps sort, nominal weight and creation time of each spool.
    """
    if spools is not None:
        live = [s for s in spools if not s.archived]
        sort_of = filament.resolve_sorts(session, live)
        return [_NewSpool(sort_of[s.id], s.label_g, s.created_at) for s in live]
    latest = session.scalar(select(func.max(FilamentSnapshot.day)))
    if latest is None:
        return []
    rows = session.scalars(select(FilamentSnapshot).where(FilamentSnapshot.day == latest))
    return [_NewSpool(r.spool_filament_id, r.label_g, r.spool_created_at) for r in rows]


def _part_receipts(session: Session, mark: ReorderMark) -> list[tuple[datetime, int]]:
    """Receipts of the part that belong to the mark, oldest first: (saved at, quantity).

    A receipt belongs to it when it was saved after the mark and its date is not before the day of
    the mark (Spec 9.4, p262). Before, the saving time alone decided: a receipt entered today but
    dated last week counted for yesterday's order. The day, not the hour: a receipt dated back has
    no real time of its own (it keeps the clock time of the saving).
    """
    rows = session.execute(
        select(JournalEntry.at, JournalEntry.booked_at, JournalEntry.qty)
        .where(
            JournalEntry.kind == "part_receipt",
            JournalEntry.part_id == mark.item_id,
            JournalEntry.at > mark.marked_at,
        )
        .order_by(JournalEntry.at, JournalEntry.id)
    )
    day = local_date(mark.marked_at)
    return [(at, qty) for at, booked_at, qty in rows if local_date(booked_at) >= day]


def _arrived(session: Session, mark: ReorderMark, seen: Sequence[_NewSpool]) -> float:
    """What of a mark has arrived, in the item's unit (Spec 9.4, p211): for a part the receipts
    after the mark, for filament the nominal weight of new spools of the sort (created after the
    mark; a new roll already in use has arrived all the same)."""
    if mark.item_type == "part":
        return float(sum(qty for _at, qty in _part_receipts(session, mark)))
    return float(sum(
        s.label_g
        for s in seen
        if s.sort_id == mark.item_id and s.created_at is not None and naive_utc(s.created_at) > mark.marked_at
    ))


def on_way(session: Session, spools: Sequence[Spool] | None = None) -> dict[tuple[str, int], float]:
    """What counts as on its way per item, in its unit: per open mark the ordered amount less what
    has arrived of it (Spec 9.4). `spools`: Bambuddy's list; None takes the latest record.

    p171: an open mark counts, also in the group of another reordered sort. p211: before, the full
    amount counted until the mark closed, so of 3 rolls ordered 2 arrived ones counted twice (in
    the stock and on its way) and a suggestion came late; a part mark closed with the first
    receipt, so the rest counted nowhere and a suggestion came early. Now what arrived is taken off.
    """
    seen = _spools_seen(session, spools)
    amounts: dict[tuple[str, int], float] = {}
    for mark in session.scalars(select(ReorderMark).where(ReorderMark.received_at.is_(None))):
        rest = max(0.0, ordered_amount(session, mark) - _arrived(session, mark, seen))
        if rest > 0:
            key = (mark.item_type, mark.item_id)
            amounts[key] = amounts.get(key, 0.0) + rest
    return amounts


def _open_marked(session: Session) -> set[tuple[str, int]]:
    rows = session.execute(
        select(ReorderMark.item_type, ReorderMark.item_id).where(ReorderMark.received_at.is_(None))
    )
    return {(kind, item) for kind, item in rows}


def _suggest(
    kind: Literal["filament", "part"], item_id: int, label: str, unit: str, *, stock: float,
    demand: float, per_day: float, lead_days: int, safety: float, pack: float, min_packs: int,
    supplier: str | None, order_url: str | None, has_history: bool = True, coming: float = 0.0,
) -> Suggestion | None:
    have = stock + coming  # what is on its way counts like stock (Spec 9.4)
    if not rules.needs_reorder(
        stock=have, demand=demand, per_day=per_day, lead_days=lead_days, safety=safety
    ):
        return None
    packs = rules.suggest_packs(
        stock=have, demand=demand, per_day=per_day, lead_days=lead_days, safety=safety,
        pack=pack, min_packs=min_packs,
    )
    return Suggestion(
        kind, item_id, label, unit, stock, demand, per_day, lead_days, safety, packs, pack,
        supplier, order_url, has_history, coming,
    )


def suggestions(
    session: Session, *, sorts: Mapping[int, filament.SortStock] | None, today: date,
    spools: Sequence[Spool] | None = None,
) -> list[Suggestion]:
    """Reorder suggestions: reordered spool sorts with settings (only when the stock is known, i.e.
    `sorts` is not None), then every active part. Items with an open mark have none (Entscheidung 14);
    what is on its way counts like stock, also in the group of another sort (Spec 9.4, p171).

    A reordered spool sort counts, over the slots where it is reordered, the stock and the usage of
    every spool sort ticked there (Spec 9.2); a sort ticked in the slots of two reordered sorts
    counts for both. `spools` tells what of an order has arrived (p211); None: the latest record.
    """
    marked = _open_marked(session)
    coming = on_way(session, spools)
    result: list[Suggestion] = []
    if sorts is not None:
        demand = filament_demand(session)
        groups = filament.reorder_groups(session)
        rows = session.execute(
            select(FilamentSettings, FilamentSort)
            .join(FilamentSort, FilamentSort.id == FilamentSettings.spool_filament_id)
            .where(FilamentSort.id.in_(list(groups)))
            .order_by(FilamentSort.label, FilamentSort.id)
        )
        by_sort = _suppliers_by_owner(session, FilamentSupplier, FilamentSupplier.sort_id)
        for settings, sort in rows:
            offers = by_sort.get(sort.id)
            if ("filament", sort.id) in marked or not offers:
                continue  # saved settings have a supplier (Spec 9.3); without one there is nothing to order
            members = groups[sort.id].members
            per_day, days = filament.consumption(session, members, today)
            have = sum(sorts[m].stock_g for m in members if m in sorts)
            sug = _with_options(
                "filament", sort.id, sort.label, "g", offers, stock=have, demand=demand.get(sort.id, 0.0),
                coming=sum(coming.get(("filament", m), 0.0) for m in members), per_day=per_day,
                safety=rules.safety_amount(settings.safety_value, settings.safety_unit, per_day),
                has_history=days > 0,
            )
            if sug is not None:
                result.append(sug)
    result += _part_suggestions(session, marked, coming, today)
    return result


def _suppliers_by_owner(session: Session, model, owner) -> dict[int, list]:
    """Each part's or spool sort's suppliers, the default first, then in the order of the form
    (Spec 9.3)."""
    found: dict[int, list] = defaultdict(list)
    for row in session.scalars(select(model).order_by(owner, model.is_default.desc(), model.position)):
        found[getattr(row, owner.key)].append(row)
    return found


def _with_options(
    kind: Literal["filament", "part"], item_id: int, label: str, unit: str,
    offers: Sequence[PartSupplier] | Sequence[FilamentSupplier], *, stock: float, demand: float,
    coming: float, per_day: float, safety: float, has_history: bool,
) -> Suggestion | None:
    """The default supplier (first of `offers`) decides with its lead time whether a suggestion
    appears; then every supplier gets its quantity by the same rule with its own lead time, pack
    size and minimum order, the default first (Spec 9.3; parts and filament alike)."""
    default = offers[0]
    sug = _suggest(
        kind, item_id, label, unit, stock=stock, demand=demand, per_day=per_day,
        lead_days=default.lead_time_days, safety=safety, pack=default.pack_size,
        min_packs=default.min_order_packs, supplier=default.name, order_url=default.order_url,
        has_history=has_history, coming=coming,
    )
    if sug is None:
        return None
    have = stock + coming
    options = tuple(
        SupplierOption(
            row.id, row.position, row.name, row.order_url,
            rules.suggest_packs(
                stock=have, demand=demand, per_day=per_day, lead_days=row.lead_time_days,
                safety=safety, pack=row.pack_size, min_packs=row.min_order_packs,
            ),
            row.pack_size, row.lead_time_days, row.is_default,
        )
        for row in offers
    )
    return replace(sug, options=options)


def _part_suggestions(
    session: Session, marked: set[tuple[str, int]], coming: Mapping[tuple[str, int], float], today: date
) -> list[Suggestion]:
    """Every active part without an open mark, with a line per supplier (Spec 9.3)."""
    result: list[Suggestion] = []
    part_needs = part_demand(session)
    part_history = session.scalar(select(func.min(JournalEntry.booked_at))) is not None
    by_part = _suppliers_by_owner(session, PartSupplier, PartSupplier.part_id)
    for part in session.scalars(
        select(Part).where(Part.archived_at.is_(None)).order_by(Part.name, Part.id)
    ):
        offers = by_part.get(part.id)
        if ("part", part.id) in marked or not offers:
            continue  # every part has a supplier (Spec 5.3, migration 0015); without one there is nothing to order
        per_day = part_consumption(session, part.id, today)
        safety = rules.reorder_floor(rules.safety_amount(part.safety_value, part.safety_unit, per_day), part.min_stock)
        sug = _with_options(
            "part", part.id, part.name, part.unit, offers, stock=part.stock, demand=part_needs.get(part.id, 0),
            coming=coming.get(("part", part.id), 0.0),  # 0 here, a part with an open mark is skipped
            per_day=per_day, safety=safety, has_history=part_history,
        )
        if sug is not None:
            result.append(sug)
    return result


@dataclass(frozen=True)
class Reach:
    """How long a reordered spool sort lasts, for the overview's filament tile (Spec 10.2, 0.4.2)."""

    sort_id: int
    label: str
    days: int | None  # None: no usage in the last 30 days
    short: bool  # it has a reorder suggestion ("knapp", Spec 9.4)
    young: bool  # less than 30 days of usage known: the reach is still rough


@dataclass(frozen=True)
class Current:
    """What the reorder page shows, computed in one place for the page and its key figure (p64)."""

    sorts: dict[int, filament.SortStock] | None
    day: date | None  # the recorded day the filament stock comes from; None when live or unknown
    suggestions: list[Suggestion]


def current(session: Session, spools: Sequence[Spool] | None, now: datetime, *, write: bool = True) -> Current:
    """Stock (live with `spools`, else the record), close arrived marks, then the suggestions.
    It writes (new sorts, closed marks): call it in a write transaction. With `write=False` it writes
    nothing, for the time writes are paused (p230): the stock comes from the record and arrived marks
    stay open until the next page that may write."""
    if not write:
        sorts, day = filament.known_sorts(session, None)
        return Current(sorts, day, suggestions(session, sorts=sorts, today=now.date()))
    sorts, day = filament.known_sorts(session, spools)
    close_arrived_marks(session, spools, now)
    return Current(sorts, day, suggestions(session, sorts=sorts, today=now.date(), spools=spools))


def filament_reach(
    session: Session, current: Current, today: date, spools: Sequence[Spool] | None = None
) -> list[Reach]:
    """Every reordered spool sort with its reach (Spec 10.2, 0.4.2), shortest first, no usage last,
    the short ones first among equals, then by name.

    The same sums as the suggestions (`suggestions`): the stock and usage of every sort ticked in the
    slots where the sort is reordered, what is on its way, the planned demand; nothing is counted twice.
    Unknown stock (`current.sorts` None): nothing. `spools` as for `on_way`.
    """
    if current.sorts is None:
        return []
    sorts = current.sorts
    demand = filament_demand(session)
    coming = on_way(session, spools)
    short = {s.item_id for s in current.suggestions if s.item_type == "filament"}
    labels = {row.id: row.label for row in session.execute(select(FilamentSort.id, FilamentSort.label))}
    found: list[Reach] = []
    for sort_id, group in filament.reorder_groups(session).items():
        members = group.members
        per_day, span = filament.consumption(session, members, today)
        days = rules.reach_days(
            stock=sum(sorts[m].stock_g for m in members if m in sorts),
            coming=sum(coming.get(("filament", m), 0.0) for m in members),
            demand=demand.get(sort_id, 0.0),
            per_day=per_day,
        )
        found.append(Reach(sort_id, labels.get(sort_id, ""), days, sort_id in short, span < filament.WINDOW_DAYS))
    return sorted(found, key=lambda r: (r.days is None, r.days or 0, not r.short, r.label.casefold(), r.sort_id))


# ---- "ordered" marks (Spec 9.4, Entscheidung 14, 17) ------------------------------------------


def _open_mark_for(session: Session, item_type: str, item_id: int) -> ReorderMark | None:
    return session.scalar(
        select(ReorderMark).where(
            ReorderMark.item_type == item_type,
            ReorderMark.item_id == item_id,
            ReorderMark.received_at.is_(None),
        )
    )


def mark_ordered(
    session: Session, item_type: str, item_id: int, packs: int, supplier_id: int | None = None,
    suppliers_seen: str | None = None,
) -> ReorderMark:
    """Mark a part or filament sort as ordered, `packs` in order units. No journal row: an order
    is no booking, and filament is never booked (Spec 9.1, 9.4).

    It is marked with one of its suppliers (`None`: the default); the mark keeps that supplier's
    pack size, name and position (Spec 9.4). `suppliers_seen` is the key of the suppliers the page
    showed (`suppliers_key`); when they changed since, nothing is marked.
    """
    if isinstance(packs, bool) or not isinstance(packs, int) or not 1 <= packs <= MAX_PACKS:
        raise ValidationError("reorder.invalid_packs")
    if item_type == "part":
        part = session.get(Part, item_id)
        known = part is not None and part.archived_at is None
    elif item_type == "filament":
        sort = session.get(FilamentSort, item_id)  # a legacy sort of 0006 is no spool sort to order
        known = sort is not None and not sort.legacy and session.get(FilamentSettings, item_id) is not None
    else:
        known = False
    if not known:
        raise NotFoundError("reorder.item_not_found")
    supplier = _default_supplier(session, item_type, item_id) if supplier_id is None else _supplier_by_id(
        session, item_type, supplier_id
    )
    owner = None if supplier is None else (supplier.part_id if item_type == "part" else supplier.sort_id)
    if owner != item_id:
        raise NotFoundError("reorder.supplier_not_found")
    if suppliers_seen and suppliers_seen != suppliers_key(session, item_type, item_id):
        raise ConflictError("reorder.suppliers_changed")
    if _open_mark_for(session, item_type, item_id) is not None:
        raise ConflictError("reorder.already_marked")
    mark = ReorderMark(
        item_type=item_type, item_id=item_id, qty=packs, pack_size=supplier.pack_size,
        supplier_name=supplier.name, supplier_position=supplier.position,
    )
    session.add(mark)
    session.flush()
    return mark


def _supplier_by_id(session: Session, item_type: str, supplier_id: int) -> PartSupplier | FilamentSupplier | None:
    return session.get(PartSupplier if item_type == "part" else FilamentSupplier, supplier_id)


def _open_mark(session: Session, mark_id: int) -> ReorderMark:
    mark = session.get(ReorderMark, mark_id)
    if mark is None:
        raise NotFoundError("reorder.mark_not_found")
    if mark.received_at is not None:
        raise ConflictError("reorder.already_withdrawn" if mark.withdrawn else "reorder.already_received")
    return mark


def mark_received(session: Session, mark_id: int) -> None:
    """The user says the order has arrived. For filament this only closes the mark: nothing is
    booked, Bambuddy keeps the spools (Spec 9.1)."""
    mark = _open_mark(session, mark_id)
    mark.received_at = utcnow()
    session.flush()


def withdraw_mark(session: Session, mark_id: int, spools: Sequence[Spool] | None = None) -> MarkRow:
    """Take an open order back, e.g. when the shop cancelled it (Spec 9.4, p280); only the user
    does this, never the automation. Returns the mark as it was, with what had arrived of it.

    The mark closes as taken back (`withdrawn`, closed at now): what is still missing no longer
    counts as on its way, a suggestion comes back when it is needed. What has arrived stays as it
    is: receipts of a part stay in its stock, new spools stay in Bambuddy. No journal row, for parts
    neither: an order is no booking, and filament is never booked (Spec 9.1). `spools`: Bambuddy's
    list for what has arrived of a filament order; None takes the latest record. The log line
    names the amount, the supplier and what had arrived (Spec 10.7).
    """
    mark = _open_mark(session, mark_id)
    row = _mark_row(session, mark, _spools_seen(session, spools))
    mark.received_at = utcnow()
    mark.withdrawn = True
    note_withdrawn(session, mark.id, row.arrived)
    session.flush()
    return row


@dataclass(frozen=True)
class MarkRow:
    id: int
    item_type: str
    item_id: int
    label: str
    packs: int
    pack_size: float
    unit: str
    marked_at: datetime
    arrived: float = 0.0  # what of it has arrived so far (p211), in `unit`
    supplier: str | None = None  # the supplier's name when marked (Spec 9.4)
    supplier_position: int | None = None  # its position then ("Supplier n" without a name)


def open_marks(session: Session, spools: Sequence[Spool] | None = None) -> list[MarkRow]:
    """The open marks (on its way), oldest first; those of archived parts are left out.
    `spools`: Bambuddy's list for what has arrived; None takes the latest record."""
    seen = _spools_seen(session, spools)
    rows: list[MarkRow] = []
    marks = session.scalars(
        select(ReorderMark)
        .where(ReorderMark.received_at.is_(None))
        .order_by(ReorderMark.marked_at, ReorderMark.id)
    )
    for mark in marks:
        if mark.item_type == "part" and session.get(Part, mark.item_id).archived_at is not None:
            continue  # archived parts leave every list (Spec 5.5); the mark stays for a restore (p60)
        rows.append(_mark_row(session, mark, seen))
    return rows


def _mark_row(session: Session, mark: ReorderMark, seen: Sequence[_NewSpool]) -> MarkRow:
    if mark.item_type == "part":
        part = session.get(Part, mark.item_id)
        label, unit = part.name, part.unit
    else:
        sort = session.get(FilamentSort, mark.item_id)
        label, unit = sort.label, "g"
    size = ordered_amount(session, mark) / mark.qty
    return MarkRow(
        mark.id, mark.item_type, mark.item_id, label, mark.qty, size, unit, mark.marked_at,
        arrived=_arrived(session, mark, seen),
        supplier=mark.supplier_name, supplier_position=mark.supplier_position,
    )


def close_arrived_marks(session: Session, spools: Sequence[Spool] | None, now: datetime) -> int:
    """Close open marks whose goods have arrived; returns how many (Entscheidung 14).

    A part mark closes when the `part_receipt` rows after the mark reach the ordered amount (p211;
    before, the first receipt closed it). A filament mark closes only with a spool list: new spools
    of the sort (created after the mark, not archived, with a creation time) must add up to at
    least the ordered amount (`ordered_amount`) of net weight.
    """
    # Reprintbuddy closes them by its rule, also when a page asks (Spec 9.4, 10.7: "Automatik").
    with acting(session, "auto"):
        return _close_arrived_marks(session, spools, now)


def _close_arrived_marks(session: Session, spools: Sequence[Spool] | None, now: datetime) -> int:
    closed = 0
    seen = None if spools is None else _spools_seen(session, spools)
    for mark in session.scalars(select(ReorderMark).where(ReorderMark.received_at.is_(None))).all():
        if mark.item_type == "part":
            # p211: the mark closes when the receipts reach the ordered amount, at the one that did
            ordered, total = ordered_amount(session, mark), 0
            for at, qty in _part_receipts(session, mark):
                total += qty
                if total >= ordered:
                    mark.received_at = at
                    closed += 1
                    break
        elif seen is not None:
            if session.get(FilamentSettings, mark.item_id) is None or session.get(FilamentSort, mark.item_id) is None:
                continue
            if _arrived(session, mark, seen) >= ordered_amount(session, mark):
                mark.received_at = now
                closed += 1
    session.flush()
    return closed
