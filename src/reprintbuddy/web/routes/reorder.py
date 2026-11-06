"""Reorder suggestions, "ordered" marks and the list "on its way" (Spec 9.3, 9.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from reprintbuddy.domain import filament, reorder
from reprintbuddy.domain.errors import DomainError, NotFoundError, ValidationError
from reprintbuddy.models import utcnow
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.filament_view import COLOR, spools_or_none
from reprintbuddy.web.formatting import format_date, to_local
from reprintbuddy.web.forms import (
    bounded_int, duplicate_redirect, error_status, error_text, saved_redirect,
)
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction

router = APIRouter()


def _number(value: float) -> str:
    return f"{value:.0f}"


async def _page(request: Request, db: Session, *, error: str | None = None, status: int = 200):
    spools = await spools_or_none(request)  # before any write transaction, never inside one
    lang = current_language(request)
    now = utcnow()
    with write_transaction(request) as tx:
        # Closing arrived marks is the page's one write; a part receipt booked while Bambuddy
        # was down still ends "on its way" (controller ruling R7).
        sorts, _day = filament.known_sorts(tx, spools)
        reorder.close_arrived_marks(tx, spools, now)
    rows = []
    for sug in reorder.suggestions(db, sorts=sorts, today=now.date()):
        entry = sorts.get(sug.item_id) if sorts is not None and sug.item_type == "filament" else None
        swatch = entry.color_hex if entry is not None else None
        total = sug.packs * sug.pack_size
        rows.append({
            "item_type": sug.item_type, "item_id": sug.item_id, "label": sug.label,
            "swatch": swatch if swatch and COLOR.fullmatch(swatch) else None,
            "stock": _number(sug.stock), "demand": _number(sug.demand),
            "per_day": f"{sug.per_day:.1f}", "lead_days": sug.lead_days,
            "safety": _number(sug.safety), "packs": sug.packs, "size": _number(sug.pack_size),
            "total": _number(total), "unit": sug.unit, "supplier": sug.supplier,
            "order_url": sug.order_url,
        })
    under_way = [
        {
            "id": m.id, "label": m.label, "packs": m.packs, "size": _number(m.pack_size),
            "unit": m.unit, "day": format_date(to_local(m.marked_at).date(), lang),
        }
        for m in reorder.open_marks(db)
    ]
    return render(
        request, "reorder.html", status_code=status, active_nav="reorder", rows=rows,
        under_way=under_way, filament_unknown=sorts is None, error=error,
    )


@router.get("/reorder")
async def reorder_page(request: Request, db: Session = Depends(get_db)):
    return await _page(request, db)


async def _act(request: Request, db: Session, work, flash_key: str):
    form = await request.form()
    try:
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            item = work(tx, form)
    except FormTokenUsed:
        return duplicate_redirect(request, "/reorder")
    except (DomainError, FormTokenInvalid) as exc:
        return await _page(
            request, db, error=error_text(current_language(request), exc), status=error_status(exc)
        )
    return saved_redirect(request, "/reorder", flash_key, item=item)


@router.post("/reorder/mark", dependencies=[Depends(verify_csrf)])
async def reorder_mark(request: Request, db: Session = Depends(get_db)):
    def work(tx, form):
        packs = bounded_int(str(form.get("packs", "")).strip())
        item_id = bounded_int(str(form.get("item_id", "")).strip())
        if packs is None:
            raise ValidationError("reorder.invalid_packs")
        if item_id is None:
            raise NotFoundError("reorder.item_not_found")
        mark = reorder.mark_ordered(tx, str(form.get("item_type", "")), item_id, packs)
        return next(m.label for m in reorder.open_marks(tx) if m.id == mark.id)

    return await _act(request, db, work, "reorder.marked")


@router.post("/reorder/marks/{mark_id:int}/received", dependencies=[Depends(verify_csrf)])
async def reorder_received(request: Request, mark_id: int, db: Session = Depends(get_db)):
    def work(tx, form):
        label = next((m.label for m in reorder.open_marks(tx) if m.id == mark_id), "")
        reorder.mark_received(tx, mark_id)
        return label

    return await _act(request, db, work, "reorder.arrived")
