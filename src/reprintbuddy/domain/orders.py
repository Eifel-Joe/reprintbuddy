"""Orders: reserve, ship the whole order, cancel (Spec 6.7).

Each order line stores the article that was chosen at order time, so a set that is
changed afterwards does not alter an open order; shipping and cancelling use the
lines only.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy.domain import rules
from reprintbuddy.domain.catalog import get_set
from reprintbuddy.domain.errors import (
    ConflictError,
    NotFoundError,
    StockLimitError,
    ValidationError,
)
from reprintbuddy.domain.stock import (
    MAX_QTY,
    Actor,
    release_for_order,
    reserve_for_order,
    ship_for_order,
)
from reprintbuddy.models import (
    Article,
    Order,
    OrderLine,
    ProductSet,
    SetPosition,
    SetPositionOption,
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
    choices: Mapping[int, int]  # position id -> chosen article id (positions with a choice only)


@dataclass(frozen=True)
class OrderGroup:
    set_name: str | None
    set_qty: int | None
    lines: list[tuple[str, int]]  # (article name, quantity)


@dataclass(frozen=True)
class OrderView:
    id: int
    reference: str
    order_date: date
    status: str
    note: str | None
    ready: bool
    groups: list[OrderGroup]


def _qty(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_QTY:
        raise ValidationError("form.invalid_number", field="orders.field.qty", min=1, max=MAX_QTY)
    return value


def _resolve(session: Session, items: Sequence[ArticleItem | SetItem]):
    """Turn the items into line specs (group_no, article_id, qty, set_id, set_qty), writing nothing."""
    specs: list[tuple[int, int, int, int | None, int | None]] = []
    for group_no, item in enumerate(items, start=1):
        qty = _qty(item.qty)
        if isinstance(item, ArticleItem):
            article = session.get(Article, item.article_id)
            if article is None:
                raise NotFoundError("articles.not_found")
            if article.archived_at is not None:
                raise ValidationError("orders.item_archived", name=article.name)
            specs.append((group_no, item.article_id, qty, None, None))
            continue
        product_set = get_set(session, item.set_id)
        if product_set.archived_at is not None:
            raise ValidationError("orders.item_archived", name=product_set.name)
        positions = session.scalars(
            select(SetPosition)
            .where(SetPosition.set_id == product_set.id)
            .order_by(SetPosition.sort, SetPosition.id)
        ).all()
        for position in positions:
            options = session.execute(
                select(Article.id, Article.name, Article.archived_at)
                .join(SetPositionOption, SetPositionOption.article_id == Article.id)
                .where(SetPositionOption.position_id == position.id)
                .order_by(Article.id)
            ).all()
            names = {a: n for a, n, _archived in options}
            active = [a for a, _n, archived in options if archived is None]
            if not active:
                raise ValidationError("orders.item_archived", name=product_set.name)
            if len(options) == 1:
                article_id = active[0]
            else:
                chosen = item.choices.get(position.id)
                if chosen is None:
                    if len(active) > 1:
                        raise ValidationError("orders.choice_required", set=product_set.name)
                    article_id = active[0]  # only one article is left to choose from
                elif chosen not in names:
                    raise ValidationError("orders.choice_invalid", set=product_set.name)
                elif chosen not in active:
                    raise ValidationError("orders.item_archived", name=names[chosen])
                else:
                    article_id = chosen
            specs.append((group_no, article_id, qty * position.qty, product_set.id, qty))
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
    items: Sequence[ArticleItem | SetItem],
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
    for group_no, article_id, qty, set_id, set_qty in specs:
        session.add(
            OrderLine(
                order_id=order.id,
                group_no=group_no,
                article_id=article_id,
                qty=qty,
                set_id=set_id,
                set_qty=set_qty,
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
) -> None:
    order = _open_order(session, order_id)
    release_for_order(session, order.id, _needed(session, order.id), actor, note)
    order.status = "cancelled"
    order.closed_at = utcnow()
    session.flush()


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
    set_names: dict[int, str] = {}
    for line, article_name in lines:
        group = groups.get(line.group_no)
        if group is None:
            name = None
            if line.set_id is not None:
                if line.set_id not in set_names:
                    set_names[line.set_id] = session.get(ProductSet, line.set_id).name
                name = set_names[line.set_id]
            group = OrderGroup(name, line.set_qty, [])
            groups[line.group_no] = group
        group.lines.append((article_name, line.qty))
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
            .limit(100)
        )
    return [_view(session, o) for o in session.scalars(query)]


def reserved_by_open_orders(session: Session) -> dict[int, int]:
    rows = session.execute(
        select(OrderLine.article_id, func.sum(OrderLine.qty))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.status == "open")
        .group_by(OrderLine.article_id)
    ).all()
    return {a: int(q) for a, q in rows}
