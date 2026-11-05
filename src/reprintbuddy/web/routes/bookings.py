"""Bookings that are not tied to an order: inventory, goods receipt of parts, sale
without order, stock-in without a print (Spec 6.2). One form and one POST route each."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy.domain import catalog, stock
from reprintbuddy.domain.errors import DomainError, NotFoundError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Article, Part
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.flash import flash
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
        articles=catalog.list_articles(db),
        parts=catalog.list_parts(db),
        rows=journal_rows(db, stock.journal_page(db, limit=20)),
        error_form=error_form,
        error=error,
        values=values or {},
        consume_checked=parse_bool((values or {}).get("consume_parts")) if error_form == "stock-in" else True,
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
        flash(request, key, **message_params)
    return RedirectResponse("/bookings", status_code=303)


@router.post("/bookings/inventory", dependencies=[Depends(verify_csrf)])
async def book_inventory(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        return {"article_id": _article_id(form), "field": str(form.get("field", "")),
                "counted": _counted(form), "note": _note(form)}

    def book(tx, p):
        _active_article(tx, p["article_id"])
        diff = stock.count_article(tx, p["article_id"], p["field"], p["counted"], "user", p["note"])
        item = tx.get(Article, p["article_id"]).name
        return [("bookings.inventory_done", {"item": item, "counted": p["counted"], "diff": f"{diff:+d}"})]

    return await _book(request, db, "inventory", prepare, book)


@router.post("/bookings/part-inventory", dependencies=[Depends(verify_csrf)])
async def book_part_inventory(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        return {"part_id": _part_id(form), "counted": _counted(form), "note": _note(form)}

    def book(tx, p):
        _active_part(tx, p["part_id"])
        diff = stock.count_part(tx, p["part_id"], p["counted"], "user", p["note"])
        item = tx.get(Part, p["part_id"]).name
        return [("bookings.inventory_done", {"item": item, "counted": p["counted"], "diff": f"{diff:+d}"})]

    return await _book(request, db, "part-inventory", prepare, book)


@router.post("/bookings/part-receipt", dependencies=[Depends(verify_csrf)])
async def book_part_receipt(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        return {"part_id": _part_id(form), "qty": _qty(form), "note": _note(form)}

    def book(tx, p):
        _active_part(tx, p["part_id"])
        stock.receive_part(tx, p["part_id"], p["qty"], "user", p["note"])
        return [("bookings.part_receipt_done", {"item": tx.get(Part, p["part_id"]).name, "qty": p["qty"]})]

    return await _book(request, db, "part-receipt", prepare, book)


@router.post("/bookings/sale", dependencies=[Depends(verify_csrf)])
async def book_sale(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        return {"article_id": _article_id(form), "qty": _qty(form), "note": _note(form)}

    def book(tx, p):
        _active_article(tx, p["article_id"])
        stock.sell_unreserved(tx, p["article_id"], p["qty"], "user", p["note"])
        return [("bookings.sale_done", {"item": tx.get(Article, p["article_id"]).name, "qty": p["qty"]})]

    return await _book(request, db, "sale", prepare, book)


@router.post("/bookings/stock-in", dependencies=[Depends(verify_csrf)])
async def book_stock_in(request: Request, db: Session = Depends(get_db)):
    def prepare(form):
        return {"article_id": _article_id(form), "qty": _qty(form), "note": _note(form),
                "consume_parts": parse_bool(form.get("consume_parts"))}

    def book(tx, p):
        _active_article(tx, p["article_id"])
        shortages = stock.stock_in(
            tx, p["article_id"], p["qty"], "user", p["note"], consume_parts=p["consume_parts"]
        )
        item = tx.get(Article, p["article_id"]).name
        return [("bookings.stock_in_done", {"item": item, "qty": p["qty"]}), *_shortages(tx, shortages)]

    return await _book(request, db, "stock-in", prepare, book)
