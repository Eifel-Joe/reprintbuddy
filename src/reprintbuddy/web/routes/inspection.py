"""Routes for inspection and assembly (Spec 8.2, 8.3): the page "To check" and the assembly page."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import inspection, stock
from reprintbuddy.domain.errors import DomainError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Article, Inspection
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.flash import flash
from reprintbuddy.web.forms import duplicate_redirect, error_status, error_text, parse_int, parse_text
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.kpis import Kpi
from reprintbuddy.web.routes.bookings import WARNING_KEYS, _active_article, _shortages

router = APIRouter()

# The most good units of one print a form accepts; the domain holds the real limit (the units).
MAX_GOOD = 1000
# An id in a form: ASCII digits only, at most 18 of them like every id in an address.
_ID = re.compile(r"[0-9]{1,18}", re.ASCII)
INSPECTION_URL = "/inspection"


def kpi_cards(lang: str, rows: list[inspection.InspectionRow]) -> list[Kpi]:
    """Prints waiting and their units (Spec 10.1: only numbers the page has)."""
    return [
        Kpi("clipboard-check", "accent" if rows else "neutral", len(rows), translate(lang, "kpi.inspections_open")),
        Kpi("package", "neutral", sum(r.units for r in rows), translate(lang, "kpi.inspections_units")),
    ]


def _inspection_page(request: Request, db: Session, *, error_inspection: int | None = None,
                     error: str | None = None, values: dict[str, str] | None = None, status: int = 200):
    rows = inspection.list_open(db)
    return render(
        request,
        "inspection.html",
        status_code=status,
        active_nav="inspection",
        rows=rows,
        cards=kpi_cards(current_language(request), rows),
        error_inspection=error_inspection,
        error=error,
        values=values or {},
    )


@router.get("/inspection")
async def inspection_page(request: Request, db: Session = Depends(get_db)):
    return _inspection_page(request, db)


# The fixed path first: "all-good" must not be read as an id (the id route only takes digits).
@router.post("/inspection/all-good", dependencies=[Depends(verify_csrf)])
async def resolve_all_good(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    # Words that are no id are dropped; the same id twice counts once.
    ids = list(dict.fromkeys(int(v) for v in form.getlist("ids") if _ID.fullmatch(str(v))))
    try:
        if not ids:
            return _inspection_page(
                request, db, error=translate(current_language(request), "inspections.none_selected"), status=200
            )
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            result = inspection.resolve_all_good(tx, ids, "user")
            messages = _shortages(tx, result.shortages)
    except FormTokenUsed:
        return duplicate_redirect(request, INSPECTION_URL)
    except (DomainError, FormTokenInvalid) as exc:
        return _inspection_page(
            request, db, error=error_text(current_language(request), exc), status=error_status(exc)
        )
    flash(request, "inspections.all_done", resolved=result.resolved, skipped=result.skipped)
    for key, message_params in messages:
        flash(request, key, warning=key in WARNING_KEYS, **message_params)
    return RedirectResponse(INSPECTION_URL, status_code=303)


@router.post("/inspection/{inspection_id:int}", dependencies=[Depends(verify_csrf)])
async def resolve_inspection(inspection_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k not in ("csrf_token", "form_token")}
    try:
        good = parse_int(form.get("good"), field="inspections.field.good", min=0, max=MAX_GOOD)
        note = parse_text(form.get("note"), field="journal.field.note", max_len=stock.MAX_NOTE)
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            result = inspection.resolve(tx, inspection_id, good, "user", note)
            name = tx.scalar(
                select(Article.name).join(Inspection, Inspection.article_id == Article.id)
                .where(Inspection.id == inspection_id)
            )
            messages = _shortages(tx, result.shortages)
    except FormTokenUsed:
        return duplicate_redirect(request, INSPECTION_URL)
    except (DomainError, FormTokenInvalid) as exc:
        return _inspection_page(
            request, db, error_inspection=inspection_id, error=error_text(current_language(request), exc),
            values=values, status=error_status(exc),
        )
    flash(request, "inspections.done", name=name, good=result.good, scrap=result.scrap)
    if result.replacement_entry_id is not None:
        flash(request, "inspections.replacement_created")
    for key, message_params in messages:
        flash(request, key, warning=key in WARNING_KEYS, **message_params)
    return RedirectResponse(INSPECTION_URL, status_code=303)


def _assembly_page(request: Request, db: Session, *, error_article: int | None = None,
                   error: str | None = None, values: dict[str, str] | None = None, status: int = 200):
    return render(
        request,
        "assembly.html",
        status_code=status,
        active_nav="assembly",
        rows=inspection.list_awaiting_assembly(db),
        error_article=error_article,
        error=error,
        values=values or {},
    )


@router.get("/assembly")
async def assembly_page(request: Request, db: Session = Depends(get_db)):
    return _assembly_page(request, db)


@router.post("/assembly/{article_id:int}", dependencies=[Depends(verify_csrf)])
async def book_assembly(article_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k not in ("csrf_token", "form_token")}
    try:
        qty = parse_int(form.get("qty"), field="bookings.field.qty", min=1, max=MAX_QTY)
        note = parse_text(form.get("note"), field="journal.field.note", max_len=stock.MAX_NOTE)
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            article = _active_article(tx, article_id)
            name = article.name
            shortages = inspection.assemble(tx, article_id, qty, "user", note)
            messages = _shortages(tx, shortages)
    except FormTokenUsed:
        return duplicate_redirect(request, "/assembly")
    except (DomainError, FormTokenInvalid) as exc:
        return _assembly_page(
            request, db, error_article=article_id, error=error_text(current_language(request), exc),
            values=values, status=error_status(exc),
        )
    flash(request, "assembly.done", name=name, n=qty)
    for key, message_params in messages:
        flash(request, key, warning=key in WARNING_KEYS, **message_params)
    return RedirectResponse("/assembly", status_code=303)
