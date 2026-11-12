"""Orders: reserve, ship the whole order, cancel (Spec 6.7).

Each order line stores the article that was chosen at order time, and for a set with
choice parts the number of the variant (Spec 6.7, p340), so a set that is changed
afterwards does not alter an open order; shipping and cancelling use the lines only.
A set that holds other sets is resolved through setresolve; each line keeps its way through
the inner sets (`inner_path`), so the order shows its articles under their inner sets as
ordered (Spec 6.7, 0.4.3 p423).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy.domain import articlesets, assembled, catalog, rules, setresolve, withdraw
from reprintbuddy.domain.errors import (
    ConflictError,
    NotFoundError,
    StockLimitError,
    ValidationError,
)
from reprintbuddy.domain.status import OrderStatus, order_status
from reprintbuddy.domain.stock import (
    MAX_QTY,
    Actor,
    consume_assembled_parts,
    release_for_order,
    reserve_for_order,
    ship_for_order,
)
from reprintbuddy.models import (
    Article,
    Order,
    OrderLine,
    ProductSet,
    utcnow,
)

MAX_REFERENCE = 100
MAX_ORDER_NOTE = 500


@dataclass(frozen=True)
class ArticleItem:
    article_id: int
    qty: int


@dataclass(frozen=True)
class SetItem:
    set_id: int
    qty: int
    key: tuple[int, ...] = ()  # the variant: the chosen article per choice field; empty without choices


@dataclass(frozen=True)
class AssembledItem:
    """A variant of an article made of parts built on order (Spec 6.7, 0.4.0 p341): its parts are
    reserved one by one, like the articles of a set."""

    article_id: int
    qty: int
    key: str = ""  # the variant: its option ids; empty without choices


@dataclass(frozen=True)
class InnerGroup:
    """An inner set of an ordered set, as ordered (Spec 6.7, 0.4.3 p423)."""

    depth: int  # 1: directly in the ordered set
    name: str
    variant_sku: str | None  # the number of its fixed variant when ordered
    qty: int  # pieces of this inner set in the order line
    lines: list[tuple[str, int]]  # the articles directly in this inner set: (name, quantity)


@dataclass(frozen=True)
class OrderGroup:
    set_name: str | None
    set_qty: int | None
    lines: list[tuple[str, int]]  # (article name, quantity); for a set the articles directly in it
    variant_sku: str | None = None  # the number of the variant as it was ordered (p340)
    assembled_name: str | None = None  # a group of an article made of parts built on order (0.4.0)
    inner: list[InnerGroup] = field(default_factory=list)  # its inner sets, in order (0.4.3 p423)


@dataclass(frozen=True)
class OrderView:
    id: int
    reference: str
    order_date: date
    status: str
    note: str | None
    ready: bool
    groups: list[OrderGroup]
    display_status: OrderStatus  # the state of the badge: open, ready, shipped or cancelled (Spec 10.1)


def _qty(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_QTY:
        raise ValidationError("form.invalid_number", field="orders.field.qty", min=1, max=MAX_QTY)
    return value


def _check_orderable(session: Session, article: Article) -> None:
    """An order line names an article sold alone or a stock holder (Spec 6.7, 5.2; 0.4.0 p341)."""
    if article.kind == articlesets.PRINT:
        if not article.sold_alone:
            raise ValidationError("orders.not_sold_alone", name=article.name)
        return
    composite = assembled.parent_of(session, article)
    if composite.assembly_mode == "order":
        raise ValidationError("orders.assembled_on_order", name=composite.name)
    if article.kind == articlesets.ASSEMBLED and assembled.options(session, article):
        raise ValidationError("orders.pick_variant", name=article.name)


def _resolve_assembled(session: Session, group_no: int, item: AssembledItem, qty: int):
    composite = session.get(Article, item.article_id)
    if composite is None or composite.kind != articlesets.ASSEMBLED:
        raise NotFoundError("articles.not_found")
    if composite.archived_at is not None:
        raise ValidationError("orders.item_archived", name=composite.name)
    if composite.assembly_mode != "order":
        raise ValidationError("orders.choice_invalid")
    row = assembled.variant_by_key(session, composite, item.key)
    needs = assembled.needs_of_key(session, composite, item.key)
    for part_id in needs:
        part = session.get(Article, part_id)
        if part.archived_at is not None:
            raise ValidationError("orders.item_archived", name=part.name)
    # The parts as they are now: the order keeps them when the parts list changes later (Spec 5.6).
    return [(group_no, part_id, qty * q, None, qty, row.sku, composite.id, None) for part_id, q in needs.items()]


def _resolve_set_lines(session: Session, defs, group_no: int, set_id: int, key: tuple[int, ...], qty: int, sku):
    """The lines of one ordered set through all its inner sets (Spec 5.4, 0.4.3 p423). An archived
    article inside (left by an earlier version, Spec 5.4 Altfall) is reserved like any other; it is
    printed again only once it is restored, since rule 7.1 leaves archived articles out."""
    specs = []
    for line in setresolve.resolve(defs, set_id, key):
        path = None
        if line.path:  # as ordered: the numbers of the inner variants now, the pieces of this order
            path = json.dumps([
                [step.set_id, setresolve.variant_number(defs, step.set_id, step.key), qty * step.units]
                for step in line.path
            ])
        specs.append((group_no, line.article_id, qty * line.qty, set_id, qty, sku, None, path))
    return specs


def _resolve(session: Session, items: Sequence[ArticleItem | SetItem | AssembledItem]):
    """Turn the items into line specs (group_no, article_id, qty, set_id, set_qty, variant_sku,
    assembled_id, inner_path), writing nothing."""
    specs: list[tuple[int, int, int, int | None, int | None, str | None, int | None, str | None]] = []
    defs = None
    for group_no, item in enumerate(items, start=1):
        qty = _qty(item.qty)
        if isinstance(item, ArticleItem):
            article = session.get(Article, item.article_id)
            if article is None:
                raise NotFoundError("articles.not_found")
            if article.archived_at is not None:
                raise ValidationError("orders.item_archived", name=article.name)
            _check_orderable(session, article)
            specs.append((group_no, item.article_id, qty, None, None, None, None, None))
            continue
        if isinstance(item, AssembledItem):
            specs.extend(_resolve_assembled(session, group_no, item, qty))
            continue
        row = catalog.set_row(session, item.set_id)
        if session.get(ProductSet, item.set_id).archived_at is not None:
            raise ValidationError("orders.item_archived", name=row.name)
        if row.incomplete is not None:
            raise ValidationError("orders.set_incomplete", set=row.name)
        key = tuple(item.key)
        if len(key) != len(row.choices) or any(
            article_id not in {o.article_id for o in field.options} for article_id, field in zip(key, row.choices)
        ):
            raise ValidationError("orders.variant_invalid", set=row.name)
        variant = next((v for v in row.variants if v.key == key), None) if key else None
        sku = variant.sku if variant is not None else None
        defs = catalog.load_defs(session) if defs is None else defs
        specs.extend(_resolve_set_lines(session, defs, group_no, row.id, key, qty, sku))
    return specs


def _aggregate(lines: Sequence[tuple[int, int]]) -> dict[int, int]:
    totals: dict[int, int] = defaultdict(int)
    for article_id, qty in lines:
        totals[article_id] += qty
    return dict(totals)


def create_order(
    session: Session,
    *,
    reference: str,
    order_date: date,
    items: Sequence[ArticleItem | SetItem | AssembledItem],
    actor: Actor,
    note: str | None = None,
) -> Order:
    """New open order; reserves every article of its lines (Spec 6.2)."""
    reference = (reference or "").strip()
    if not reference:
        raise ValidationError("orders.reference_required")
    if len(reference) > MAX_REFERENCE:
        raise ValidationError("form.too_long", field="orders.field.reference", max=MAX_REFERENCE)
    note = (note or "").strip() or None
    if note is not None and len(note) > MAX_ORDER_NOTE:
        raise ValidationError("form.too_long", field="orders.field.note", max=MAX_ORDER_NOTE)
    if not items:
        raise ValidationError("orders.empty")
    specs = _resolve(session, items)

    order = Order(reference=reference, order_date=order_date, status="open", note=note)
    session.add(order)
    session.flush()
    for group_no, article_id, qty, set_id, set_qty, variant_sku, assembled_id, inner_path in specs:
        session.add(
            OrderLine(
                order_id=order.id,
                group_no=group_no,
                article_id=article_id,
                qty=qty,
                set_id=set_id,
                set_qty=set_qty,
                variant_sku=variant_sku,
                assembled_id=assembled_id,
                inner_path=inner_path,
            )
        )
    session.flush()
    reserve_for_order(
        session, order.id, _aggregate([(s[1], s[2]) for s in specs]), actor, note=note
    )
    return order


def _open_order(session: Session, order_id: int) -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise NotFoundError("orders.not_found")
    if order.status != "open":
        raise ConflictError("orders.not_open")
    return order


def _needed(session: Session, order_id: int) -> dict[int, int]:
    rows = session.execute(
        select(OrderLine.article_id, OrderLine.qty).where(OrderLine.order_id == order_id)
    ).all()
    return _aggregate([(a, q) for a, q in rows])


def cancel_order(
    session: Session, order_id: int, actor: Actor, note: str | None = None
) -> withdraw.WithdrawReport:
    """Release the reservations, then take back what is now planned over the target (Spec 6.2, 7.2a):
    rule entries of Reprintbuddy's queue at once, waiting prints in Bambuddy noted for the engine."""
    order = _open_order(session, order_id)
    release_for_order(session, order.id, _needed(session, order.id), actor, note)
    order.status = "cancelled"
    order.closed_at = utcnow()
    session.flush()
    return withdraw.withdraw_after_cancel(session, order.id)


def _stock_of(session: Session, article_ids: Sequence[int]) -> dict[int, int]:
    rows = session.execute(
        select(Article.id, Article.stock).where(Article.id.in_(list(article_ids)))
    ).all()
    return {a: s for a, s in rows}


def ship_order(session: Session, order_id: int, actor: Actor, note: str | None = None) -> None:
    """Ship the whole order; only when every article is in stock (Spec 6.7)."""
    order = _open_order(session, order_id)
    needed = _needed(session, order.id)
    if not rules.order_ready(needed, _stock_of(session, list(needed))):
        raise StockLimitError("orders.not_ready")
    ship_for_order(session, order.id, needed, actor, note)
    # Shipping an article made of parts built on order is its assembly: its purchased parts go too.
    groups = session.execute(
        select(OrderLine.group_no, OrderLine.assembled_id, OrderLine.set_qty, OrderLine.variant_sku)
        .where(OrderLine.order_id == order.id, OrderLine.assembled_id.is_not(None))
        .distinct()
        .order_by(OrderLine.group_no)
    ).all()
    for _group_no, assembled_id, units, variant_sku in groups:
        consume_assembled_parts(session, assembled_id, units, order.id, actor, variant_sku=variant_sku)
    order.status = "shipped"
    order.closed_at = utcnow()
    session.flush()


def _view(session: Session, order: Order) -> OrderView:
    lines = session.execute(
        select(OrderLine, Article.name)
        .join(Article, Article.id == OrderLine.article_id)
        .where(OrderLine.order_id == order.id)
        .order_by(OrderLine.group_no, OrderLine.id)
    ).all()
    groups: dict[int, OrderGroup] = {}
    inner: dict[tuple[int, tuple[int, ...]], InnerGroup] = {}
    set_names: dict[int, str] = {}

    def set_name(set_id: int) -> str:
        if set_id not in set_names:
            set_names[set_id] = session.get(ProductSet, set_id).name
        return set_names[set_id]

    for line, article_name in lines:
        group = groups.get(line.group_no)
        if group is None:
            name = set_name(line.set_id) if line.set_id is not None else None
            assembled_name = None
            if line.assembled_id is not None:
                assembled_name = session.get(Article, line.assembled_id).name
            group = OrderGroup(name, line.set_qty, [], line.variant_sku, assembled_name)
            groups[line.group_no] = group
        if not line.inner_path:
            group.lines.append((article_name, line.qty))
            continue
        # Each level of the way gets its group once, in the order the lines were written (depth
        # first, so a set comes before the sets inside it); a level without own articles stays empty.
        path = json.loads(line.inner_path)
        for depth in range(1, len(path) + 1):
            prefix = (line.group_no, tuple(step[0] for step in path[:depth]))
            if prefix not in inner:
                step_set, step_sku, step_units = path[depth - 1]
                inner[prefix] = InnerGroup(depth, set_name(step_set), step_sku, step_units, [])
                group.inner.append(inner[prefix])
        inner[prefix].lines.append((article_name, line.qty))
    needed = _needed(session, order.id)
    ready = order.status == "open" and rules.order_ready(needed, _stock_of(session, list(needed)))
    return OrderView(
        id=order.id,
        reference=order.reference,
        order_date=order.order_date,
        status=order.status,
        note=order.note,
        ready=ready,
        groups=[groups[k] for k in sorted(groups)],
        display_status=order_status(status=order.status, ready=ready),
    )


def get_order(session: Session, order_id: int) -> OrderView:
    order = session.get(Order, order_id)
    if order is None:
        raise NotFoundError("orders.not_found")
    return _view(session, order)


def list_orders(session: Session, view: Literal["open", "closed"]) -> list[OrderView]:
    if view == "open":
        query = (
            select(Order).where(Order.status == "open").order_by(Order.order_date, Order.id)
        )
    else:
        query = (
            select(Order)
            .where(Order.status != "open")
            .order_by(Order.closed_at.desc(), Order.id.desc())
        )  # all of them: the page searches and counts the whole view (Spec 10.1)
    return [_view(session, o) for o in session.scalars(query)]


def reserved_by_open_orders(session: Session) -> dict[int, int]:
    rows = session.execute(
        select(OrderLine.article_id, func.sum(OrderLine.qty))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.status == "open")
        .group_by(OrderLine.article_id)
    ).all()
    return {a: int(q) for a, q in rows}
