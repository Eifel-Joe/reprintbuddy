"""Orders: open list, new order (also of a set's variant), ship, cancel (Spec 6.7)."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from reprintbuddy.domain import catalog, orders, seen, stock
from reprintbuddy.domain.errors import DomainError, ValidationError
from reprintbuddy.domain.orders import ArticleItem, AssembledItem, SetItem
from reprintbuddy.web import assembled_view
from reprintbuddy.domain.status import summarize
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import utcnow
from reprintbuddy.web import kpis, listing, regions
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
from reprintbuddy.web.filament_view import may_write
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction
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
    if not closed and regions.page_load(request) and may_write(request):
        # Opening the open orders clears "new" on the overview (Spec 10.2); a live refetch does not.
        with write_transaction(request) as tx:
            seen.mark_orders(tx)
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


def _parse_item(raw: str) -> tuple[str, int, tuple[int, ...]]:
    """"article:<id>", "set:<id>" or, for a variant, "set:<id>:<article id per choice field>"."""
    kind, _, rest = raw.partition(":")
    numbers = [bounded_int(part) for part in rest.split(":")]
    if (
        kind not in ("article", "set", "assembled")
        or any(number is None or number > MAX_ID for number in numbers)
        or len(numbers) > (1 + catalog.CHOICE_FIELDS if kind in ("set", "assembled") else 1)
    ):
        raise ValidationError("orders.invalid_item")
    return kind, numbers[0], tuple(numbers[1:])  # type: ignore[arg-type]


def _set_options(db: Session) -> list[tuple[str, str]]:
    """What the form offers under "Sets": a set without choices by its name, a set with choice parts
    once per variant, "Starter Set · EX-S10-A (Plate A)" (Spec 6.7, p340). Sets that cannot be
    ordered and variants of archived choice parts are left out."""
    options = []
    for row in catalog.list_sets(db):
        if not row.orderable:
            continue
        if not row.choices:
            options.append((f"set:{row.id}", row.name))
            continue
        for variant in row.variants:
            value = ":".join(["set", str(row.id), *(str(a) for a in variant.key)])
            options.append((value, f"{row.name} · {variant.sku} ({', '.join(variant.names)})"))
    return options


def _form_page(request, db, *, form_values, items, rows, error=None, status=200):
    return render(
        request,
        "order_new.html",
        status_code=status,
        active_nav="orders",
        values=form_values,
        items=items,
        rows=rows,
        articles=assembled_view.order_choices(db),  # sold alone, stock holders, variants on order (0.4.0)
        sets=_set_options(db),
        error=error,
    )


@router.get("/orders/new")
async def orders_new(request: Request, db: Session = Depends(get_db)):
    values = {"reference": "", "order_date": _today(), "note": ""}
    items = [{"item": "", "qty": "1"} for _ in range(DEFAULT_ROWS)]
    return _form_page(request, db, form_values=values, items=items, rows=DEFAULT_ROWS)


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
        return _form_page(request, db, form_values=values, items=items, rows=rows, error=error, status=status)

    if more:  # show more rows; nothing is booked and the token stays unused
        return page()
    try:
        order_date = _parse_date(form.get("order_date"))
        order_items: list[ArticleItem | SetItem | AssembledItem] = []
        for row in items:
            if not row["item"].strip():
                continue
            kind, ident, key = _parse_item(row["item"].strip())
            qty = parse_int(row["qty"], field="orders.field.qty", min=1, max=MAX_QTY)
            if kind == "assembled":
                order_items.append(AssembledItem(ident, qty, ":".join(str(k) for k in key)))
            else:
                order_items.append(ArticleItem(ident, qty) if kind == "article" else SetItem(ident, qty, key))
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
