"""Routes for inspection and assembly (Spec 8.2, 8.3); for now only the assembly routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy.domain import inspection, stock
from reprintbuddy.domain.errors import DomainError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.flash import flash
from reprintbuddy.web.forms import duplicate_redirect, error_status, error_text, parse_int, parse_text
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction
from reprintbuddy.web.routes.bookings import WARNING_KEYS, _active_article, _shortages

router = APIRouter()


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
