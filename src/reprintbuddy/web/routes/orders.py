"""Orders: open list, new order (also with set choices), ship, cancel (Spec 6.7)."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import catalog, orders, stock
from reprintbuddy.domain.errors import DomainError, ValidationError
from reprintbuddy.domain.orders import ArticleItem, SetItem
from reprintbuddy.domain.status import summarize
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Article, SetPosition, SetPositionOption, utcnow
from reprintbuddy.web import kpis, listing
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, local_next, render
from reprintbuddy.web.formatting import to_local
from reprintbuddy.web.forms import (
    bounded_int,
    duplicate_redirect,
    error_status,
    error_text,
    parse_int,
    parse_text,
    saved_redirect,
)
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction
from reprintbuddy.web.i18n import translate

router = APIRouter()

DEFAULT_ROWS = 5
MORE_ROWS = 5
MAX_ROWS = 50
MAX_ID = 2**31 - 1


# ---- list ------------------------------------------------------------------------------


# The states of each view, in the order of the filters; the summary names the ones after the total.
_OPEN_STATES = ("open", "ready")
_CLOSED_STATES = ("shipped", "cancelled")


@router.get("/orders")
async def orders_list(
    request: Request, view: str = "open", q: str | None = None, db: Session = Depends(get_db)
):
    """Overview with key figure cards, search on the reference, status filter and tiles or list
    (Spec 10.1). The cards count all orders of the view; the filter counts follow the search;
    the list shows what passes both."""
    if (redirect := listing.remember_layout(request, "orders")) is not None:
        return redirect  # ?layout=... only sets the cookie
    closed = view == "closed"
    states = _CLOSED_STATES if closed else _OPEN_STATES
    status = listing.pick(request.query_params.get("status"), states)
    every = orders.list_orders(db, "closed" if closed else "open")
    found = [row for row in every if listing.matches(q, row.reference)]
    counts = Counter(row.display_status for row in found)
    shown = _CLOSED_STATES if closed else ("ready",)  # "open" is the total itself
    lang = current_language(request)
    t = lambda key: translate(lang, key)  # noqa: E731
    cards = kpis.state_cards(
        t, "order", "receipt", len(every),
        t("kpi.orders_closed" if closed else "kpi.orders_open"),
        summarize((row.display_status for row in every), shown),
    )
    if not closed:  # open orders wait for something: the total is a warning, not the accent
        cards[0] = replace(cards[0], tone="warn")
    keep = (("status", status), ("view", "closed" if closed else None))
    layout = listing.layout_of(request, "orders")
    return render(
        request,
        "orders_list.html",
        active_nav="orders",
        closed=closed,
        rows=[row for row in found if status is None or row.display_status == status],
        total=len(every),
        kpis=cards,
        filters=listing.filter_links(request, states, counts, len(found), status),
        layout=layout,
        layout_links=listing.layout_links(request, layout),
        q=q,
        status=status,
        search_keep=[(name, value) for name, value in keep if value],  # a new search keeps these
    )


# ---- new order -----------------------------------------------------------------------------


def _rows_of(form, *, more: bool) -> int:
    raw = str(form.get("rows", "")).strip()
    value = bounded_int(raw)
    rows = value if value is not None and 1 <= value <= MAX_ROWS else DEFAULT_ROWS
    return min(rows + MORE_ROWS, MAX_ROWS) if more else rows


def _today() -> str:
    return to_local(utcnow()).date().isoformat()


def _parse_date(raw: str | None) -> date:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        raise ValidationError("form.invalid_date", field="orders.field.date") from None


def _typed_items(form, rows: int) -> list[dict[str, str]]:
    return [
        {"item": str(form.get(f"item_{i}", "")), "qty": str(form.get(f"qty_{i}", "1"))}
        for i in range(rows)
    ]


def _parse_item(raw: str) -> tuple[str, int]:
    kind, _, ident = raw.partition(":")
    number = bounded_int(ident)
    if kind not in ("article", "set") or number is None or number > MAX_ID:
        raise ValidationError("orders.invalid_item")
    return kind, number


def _choice_blocks(db: Session, form, items: list[dict[str, str]]) -> dict[int, list[dict[str, object]]]:
    """For each set row: the positions with a choice and what was chosen so far."""
    blocks: dict[int, list[dict[str, object]]] = {}
    for i, row in enumerate(items):
        kind, _, ident = row["item"].partition(":")
        set_id = bounded_int(ident)
        if kind != "set" or set_id is None or set_id > MAX_ID:
            continue
        positions = db.scalars(
            select(SetPosition).where(SetPosition.set_id == set_id).order_by(SetPosition.sort, SetPosition.id)
        ).all()
        found = []
        for position in positions:
            articles = db.execute(
                select(Article.id, Article.name)
                .join(SetPositionOption, SetPositionOption.article_id == Article.id)
                .where(SetPositionOption.position_id == position.id, Article.archived_at.is_(None))
                .order_by(Article.name, Article.id)
            ).all()
            if len(articles) > 1:
                chosen = str(form.get(f"choice_{i}_{position.id}", ""))
                found.append({"position_id": position.id, "qty": position.qty, "options": articles, "chosen": chosen})
        if found:
            blocks[i] = found
    return blocks


def _form_page(request, db, *, form_values, items, rows, blocks, error=None, status=200):
    return render(
        request,
        "order_new.html",
        status_code=status,
        active_nav="orders",
        values=form_values,
        items=items,
        rows=rows,
        blocks=blocks,
        articles=catalog.list_articles(db),
        sets=[row for row in catalog.list_sets(db) if row.orderable],
        error=error,
    )


@router.get("/orders/new")
async def orders_new(request: Request, db: Session = Depends(get_db)):
    values = {"reference": "", "order_date": _today(), "note": ""}
    items = [{"item": "", "qty": "1"} for _ in range(DEFAULT_ROWS)]
    return _form_page(request, db, form_values=values, items=items, rows=DEFAULT_ROWS, blocks={})


@router.post("/orders/new", dependencies=[Depends(verify_csrf)])
async def orders_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    more = form.get("action") == "more"
    rows = _rows_of(form, more=more)
    values = {
        "reference": str(form.get("reference", "")),
        "order_date": str(form.get("order_date", "")),
        "note": str(form.get("note", "")),
    }
    items = _typed_items(form, rows)
    lang = current_language(request)

    def page(error=None, status=200):
        return _form_page(
            request, db, form_values=values, items=items, rows=rows,
            blocks=_choice_blocks(db, form, items), error=error, status=status,
        )

    if more:  # show more rows; nothing is booked and the token stays unused
        return page()
    try:
        order_date = _parse_date(form.get("order_date"))
        parsed: list[tuple[int, str, int, int]] = []  # (row, kind, id, qty)
        for i, row in enumerate(items):
            if not row["item"].strip():
                continue
            kind, ident = _parse_item(row["item"].strip())
            qty = parse_int(row["qty"], field="orders.field.qty", min=1, max=MAX_QTY)
            parsed.append((i, kind, ident, qty))
        blocks = _choice_blocks(db, form, items)
        missing = any(
            not block["chosen"] for i, kind, _id, _q in parsed if kind == "set" for block in blocks.get(i, [])
        )
        if missing:
            return page(error=translate(lang, "orders.choose"))
        order_items: list[ArticleItem | SetItem] = []
        for i, kind, ident, qty in parsed:
            if kind == "article":
                order_items.append(ArticleItem(ident, qty))
            else:
                choices = {}
                for block in blocks.get(i, []):
                    chosen = bounded_int(str(block["chosen"]))
                    choices[int(block["position_id"])] = chosen if chosen is not None and chosen <= MAX_ID else -1
                order_items.append(SetItem(ident, qty, choices))
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            order = orders.create_order(
                tx, reference=values["reference"], order_date=order_date, items=order_items,
                actor="user", note=values["note"],
            )
            order_id, reference = order.id, order.reference
    except FormTokenUsed:
        return duplicate_redirect(request, "/orders")
    except (DomainError, FormTokenInvalid) as exc:
        return page(error=error_text(lang, exc), status=error_status(exc))
    return saved_redirect(request, f"/orders/{order_id}", "orders.created", reference=reference)


# ---- detail, ship, cancel ---------------------------------------------------------------------


def _detail_view(db: Session, order_id: int):
    try:
        return orders.get_order(db, order_id)
    except DomainError:
        raise HTTPException(status_code=404) from None


def _detail_page(request: Request, db: Session, order_id: int, error: str | None = None, status: int = 200):
    view = _detail_view(db, order_id)
    return render(request, "order_detail.html", status_code=status, active_nav="orders", order=view, error=error)


@router.get("/orders/{order_id:int}")
async def order_detail(request: Request, order_id: int, db: Session = Depends(get_db)):
    return _detail_page(request, db, order_id)


async def _act(request: Request, order_id: int, db: Session, action, flash_key: str):
    form = await request.form()
    after = local_next(form) or f"/orders/{order_id}"
    try:
        orders.get_order(db, order_id)
    except DomainError:
        raise HTTPException(status_code=404) from None
    try:
        note = parse_text(form.get("note"), field="journal.field.note", max_len=stock.MAX_NOTE)
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            action(tx, order_id, "user", note)
            reference = orders.get_order(tx, order_id).reference
    except FormTokenUsed:
        return duplicate_redirect(request, after)
    except (DomainError, FormTokenInvalid) as exc:
        return _detail_page(
            request, db, order_id, error=error_text(current_language(request), exc), status=error_status(exc)
        )
    return saved_redirect(request, after, flash_key, reference=reference)


@router.post("/orders/{order_id:int}/ship", dependencies=[Depends(verify_csrf)])
async def order_ship(request: Request, order_id: int, db: Session = Depends(get_db)):
    return await _act(request, order_id, db, orders.ship_order, "orders.shipped")


@router.post("/orders/{order_id:int}/cancel", dependencies=[Depends(verify_csrf)])
async def order_cancel(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Cancelling takes two steps without JavaScript: the first post shows a confirmation
    (nothing is booked, the one-time token stays unused); the second one carries `confirm`."""
    form = await request.form()
    if form.get("confirm") != "yes":
        view = _detail_view(db, order_id)
        if view.status != "open":
            return _detail_page(
                request, db, order_id, error=translate(current_language(request), "orders.not_open")
            )
        return render(
            request, "order_cancel.html", active_nav="orders", order=view,
            note=str(form.get("note", "")), form_token_value=str(form.get("form_token", "")),
            next_url=local_next(form),
        )
    return await _act(request, order_id, db, orders.cancel_order, "orders.cancelled")
