"""Bookings that are not tied to an order: inventory, goods receipt of parts, sale
without order, stock-in without a print (Spec 6.2). One form and one POST route each."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy.domain import assembled, catalog, stock
from reprintbuddy.domain.errors import DomainError, NotFoundError, ValidationError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.localtime import at_local_time, end_of_local_day
from reprintbuddy.models import Article, Part, utcnow
from reprintbuddy.web import assembled_view
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.flash import flash
from reprintbuddy.web.formatting import format_date, to_local
from reprintbuddy.web.forms import duplicate_redirect, error_status, error_text, parse_bool, parse_int, parse_text
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction
from reprintbuddy.web.journal_view import journal_rows

router = APIRouter()

MAX_ID = 2**31 - 1


def _page(request: Request, db: Session, *, error_form: str | None = None, error: str | None = None,
          values: dict[str, str] | None = None, status: int = 200):
    return render(
        request,
        "bookings.html",
        status_code=status,
        active_nav="bookings",
        # 0.4.0 (p341): count and sale take stock holders, sale also variants built on order,
        # stock in only print articles (web/assembled_view.py)
        count_options=assembled_view.count_choices(db),
        stock_only=assembled_view.stock_only_values(db),  # p362: no "awaiting assembly" for these
        sale_options=assembled_view.sale_choices(db),
        stock_in_options=assembled_view.stock_in_choices(db),
        parts=catalog.list_parts(db),
        rows=journal_rows(db, stock.journal_page(db, limit=20)),
        error_form=error_form,
        error=error,
        values=values or {},
        consume_checked=parse_bool((values or {}).get("consume_parts")) if error_form == "stock-in" else True,
        today=f"{_local_today():%Y-%m-%d}",  # the date field's default and latest day (p172)
    )


@router.get("/bookings")
async def bookings_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db)


def _article_id(form) -> int:
    return parse_int(form.get("article_id"), field="bookings.field.article", min=1, max=MAX_ID)


def _part_id(form) -> int:
    return parse_int(form.get("part_id"), field="bookings.field.part", min=1, max=MAX_ID)


def _qty(form) -> int:
    return parse_int(form.get("qty"), field="bookings.field.qty", min=1, max=MAX_QTY)


def _counted(form) -> int:
    return parse_int(form.get("counted"), field="bookings.field.counted", min=0, max=MAX_QTY)


def _note(form) -> str | None:
    return parse_text(form.get("note"), field="journal.field.note", max_len=stock.MAX_NOTE)


EARLIEST = date(2000, 1, 1)  # catches a mistyped year; nothing older can be booked meaningfully


def _local_today() -> date:
    return to_local(utcnow()).date()


def _booked_at(form, *, day_end: bool = False) -> datetime | None:
    """The date of a hand booking (Spec 6.2, p172): today by default, earlier days allowed, no
    future day. None books it when it is saved: today, or a form without the field (a page loaded
    before this version). An earlier day counts at the same clock time on that day, so the journal
    keeps an order within the day; `at` keeps the real saving time.

    `day_end` (inventory, p260): an earlier day counts at its end - a count dated back is usually
    the count of that evening, so every booking of that day lies in it. Before, it counted at the
    clock time of typing it in, and a sale later that day was played forward after the count."""
    if "booked_on" not in form:
        return None
    try:
        day = date.fromisoformat(str(form.get("booked_on", "")).strip())
    except ValueError:
        raise ValidationError("form.invalid_date", field="bookings.field.date") from None
    if day < EARLIEST:
        raise ValidationError("form.invalid_date", field="bookings.field.date")
    now = to_local(utcnow())
    if day > now.date():
        raise ValidationError("bookings.date_future")
    if day == now.date():
        return None
    if day_end:
        return end_of_local_day(day)
    return at_local_time(day, now.time())


def _active_article(tx: Session, article_id: int) -> Article:
    """Bookings only take active articles; an archived one counts as unknown."""
    article = tx.get(Article, article_id)
    if article is None or article.archived_at is not None:
        raise NotFoundError("articles.not_found")
    return article


def _active_part(tx: Session, part_id: int) -> Part:
    part = tx.get(Part, part_id)
    if part is None or part.archived_at is not None:
        raise NotFoundError("parts.not_found")
    return part


# Messages that report trouble although the booking went through.
WARNING_KEYS = frozenset({"stock.parts_short"})


def _shortages(tx: Session, shortages: list[stock.PartShortage]) -> list[tuple[str, dict[str, Any]]]:
    messages = []
    for shortage in shortages:
        part = tx.get(Part, shortage.part_id)
        messages.append(("stock.parts_short", {"part": part.name, "had": shortage.had, "needed": shortage.needed}))
    return messages


async def _book(
    request: Request,
    db: Session,
    name: str,
    prepare: Callable[[Any], dict[str, Any]],
    book: Callable[[Session, dict[str, Any]], list[tuple[str, dict[str, Any]]]],
):
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k not in ("csrf_token", "form_token")}
    try:
        params = prepare(form)
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            messages = book(tx, params)
    except FormTokenUsed:
        return duplicate_redirect(request, "/bookings")
    except (DomainError, FormTokenInvalid) as exc:
        return _page(
            request, db, error_form=name, error=error_text(current_language(request), exc),
            values=values, status=error_status(exc),
        )
    for key, message_params in messages:
        flash(request, key, warning=key in WARNING_KEYS, **message_params)
    return RedirectResponse("/bookings", status_code=303)


def _inventory_done(request: Request, item: str, p: dict, diff: int) -> tuple[str, dict]:
    """The message of an inventory; dated back it names the day and the value of then (p210)."""
    params = {"item": item, "counted": p["counted"], "diff": f"{diff:+d}"}
    if p["booked_at"] is None:
        return "bookings.inventory_done", params
    day = format_date(to_local(p["booked_at"]).date(), current_language(request))
    return "bookings.inventory_done_then", {**params, "day": day, "then": p["counted"] - diff}


@router.post("/bookings/inventory", dependencies=[Depends(verify_csrf)])
async def book_inventory(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        return {"article_id": _article_id(form), "field": str(form.get("field", "")),
                "counted": _counted(form), "note": _note(form), "booked_at": _booked_at(form, day_end=True)}

    def book(tx, p):
        _active_article(tx, p["article_id"])
        diff = stock.count_article(
            tx, p["article_id"], p["field"], p["counted"], "user", p["note"], booked_at=p["booked_at"]
        )
        return [_inventory_done(request, tx.get(Article, p["article_id"]).name, p, diff)]

    return await _book(request, db, "inventory", prepare, book)


@router.post("/bookings/part-inventory", dependencies=[Depends(verify_csrf)])
async def book_part_inventory(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        return {"part_id": _part_id(form), "counted": _counted(form), "note": _note(form),
                "booked_at": _booked_at(form, day_end=True)}

    def book(tx, p):
        _active_part(tx, p["part_id"])
        diff = stock.count_part(tx, p["part_id"], p["counted"], "user", p["note"], booked_at=p["booked_at"])
        return [_inventory_done(request, tx.get(Part, p["part_id"]).name, p, diff)]

    return await _book(request, db, "part-inventory", prepare, book)


@router.post("/bookings/part-receipt", dependencies=[Depends(verify_csrf)])
async def book_part_receipt(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        return {"part_id": _part_id(form), "qty": _qty(form), "note": _note(form),
                "booked_at": _booked_at(form)}

    def book(tx, p):
        _active_part(tx, p["part_id"])
        stock.receive_part(tx, p["part_id"], p["qty"], "user", p["note"], booked_at=p["booked_at"])
        return [("bookings.part_receipt_done", {"item": tx.get(Part, p["part_id"]).name, "qty": p["qty"]})]

    return await _book(request, db, "part-receipt", prepare, book)


@router.post("/bookings/sale", dependencies=[Depends(verify_csrf)])
async def book_sale(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        built_on_order = assembled_view.parse_assembled(str(form.get("article_id", "")))
        return {"article_id": built_on_order[0] if built_on_order else _article_id(form),
                "key": built_on_order[1] if built_on_order else None,
                "qty": _qty(form), "note": _note(form), "booked_at": _booked_at(form)}

    def book(tx, p):
        _active_article(tx, p["article_id"])
        if p["key"] is not None:  # a variant built on order: its parts go at once (Teil C 4)
            shortages = assembled.sell(tx, p["article_id"], p["key"], p["qty"], "user", p["note"], booked_at=p["booked_at"])
            return [("bookings.sale_done", {"item": tx.get(Article, p["article_id"]).name, "qty": p["qty"]}),
                    *_shortages(tx, shortages)]
        stock.sell_unreserved(tx, p["article_id"], p["qty"], "user", p["note"], booked_at=p["booked_at"])
        return [("bookings.sale_done", {"item": tx.get(Article, p["article_id"]).name, "qty": p["qty"]})]

    return await _book(request, db, "sale", prepare, book)


@router.post("/bookings/stock-in", dependencies=[Depends(verify_csrf)])
async def book_stock_in(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        return {"article_id": _article_id(form), "qty": _qty(form), "note": _note(form),
                "consume_parts": parse_bool(form.get("consume_parts")), "booked_at": _booked_at(form)}

    def book(tx, p):
        _active_article(tx, p["article_id"])
        shortages = stock.stock_in(
            tx, p["article_id"], p["qty"], "user", p["note"], consume_parts=p["consume_parts"],
            booked_at=p["booked_at"],
        )
        item = tx.get(Article, p["article_id"]).name
        return [("bookings.stock_in_done", {"item": item, "qty": p["qty"]}), *_shortages(tx, shortages)]

    return await _book(request, db, "stock-in", prepare, book)
