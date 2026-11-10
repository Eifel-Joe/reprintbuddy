"""Reorder suggestions, "ordered" marks and the list "on its way" (Spec 9.3, 9.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from reprintbuddy.domain import reorder
from reprintbuddy.domain.errors import DomainError, NotFoundError, ValidationError
from reprintbuddy.models import utcnow
from reprintbuddy.web import kpis
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.filament_view import COLOR, spools_or_none
from reprintbuddy.web.formatting import format_date, to_local, unit_label
from reprintbuddy.web.forms import (
    bounded_int, duplicate_redirect, error_status, error_text, saved_redirect,
)
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction

router = APIRouter()


def _number(value: float) -> str:
    return f"{value:.0f}"


def _supplier_options(sug: reorder.Suggestion, lang: str) -> list[dict[str, object]]:
    """The lines of a part with several suppliers, default first (Spec 9.3); one supplier keeps
    the single detail line, as before."""
    if len(sug.options) < 2:
        return []
    unit = unit_label(sug.unit, lang)
    return [
        {
            "id": option.supplier_id, "is_default": option.is_default, "order_url": option.order_url,
            "packs": option.packs,
            "label": option.name or translate(lang, "parts.supplier_unnamed", n=option.position + 1),
            "text": translate(
                lang, "reorder.suggest_part", packs=option.packs, size=_number(option.pack_size),
                total=_number(option.packs * option.pack_size), unit=unit,
            ),
        }
        for option in sug.options
    ]


async def _page(
    request: Request, db: Session, *, error: str | None = None, status: int = 200,
    typed: dict[str, str] | None = None,
):
    spools = await spools_or_none(request)  # before any write transaction, never inside one
    lang = current_language(request)
    now = utcnow()
    with write_transaction(request) as tx:
        # Entering new spool sorts and closing arrived marks are the page's writes; a part receipt
        # booked while Bambuddy was down still ends "on its way" (controller ruling R7). The key
        # figure of the overview counts with the same function (p64).
        state = reorder.current(tx, spools, now)
    sorts, day = state.sorts, state.day
    rows = []
    for sug in state.suggestions:
        entry = sorts.get(sug.item_id) if sorts is not None and sug.item_type == "filament" else None
        swatch = entry.color_hex if entry is not None else None
        total = sug.packs * sug.pack_size
        mine = typed is not None and (typed["item_type"], typed["item_id"]) == (sug.item_type, str(sug.item_id))
        options = _supplier_options(sug, lang)
        chosen = typed["supplier_id"] if mine and typed["supplier_id"] else next(
            (str(o["id"]) for o in options if o["is_default"]), ""
        )
        rows.append({
            "item_type": sug.item_type, "item_id": sug.item_id, "label": sug.label,
            "swatch": swatch if swatch and COLOR.fullmatch(swatch) else None,
            "stock": _number(sug.stock), "demand": _number(sug.demand),
            "on_way": _number(sug.on_way) if sug.on_way > 0 else None,  # p171: it counts, so it shows
            "per_day": sug.per_day if sug.has_history else None,  # p59: no "0.0 g/day" without history
            "packs_value": typed["packs"] if mine else sug.packs, "lead_days": sug.lead_days,
            "options": options, "chosen": chosen,
            "safety": _number(sug.safety), "packs": sug.packs, "size": _number(sug.pack_size),
            "total": _number(total),
            "suggestion": translate(lang, "reorder.suggest_part", packs=sug.packs, size=_number(sug.pack_size), total=_number(total), unit=unit_label(sug.unit, lang))
            if sug.item_type == "part"
            else translate(lang, "reorder.suggest_filament", packs=sug.packs, size=_number(sug.pack_size), unit=unit_label(sug.unit, lang)), "unit": sug.unit, "supplier": sug.supplier,
            "order_url": sug.order_url,
        })
    under_way = [
        {
            "id": m.id, "label": m.label, "packs": m.packs, "size": _number(m.pack_size),
            "unit": m.unit, "day": format_date(to_local(m.marked_at).date(), lang), "supplier": m.supplier,
        }
        for m in reorder.open_marks(db)
    ]
    # The key figures count the tiles and the marks the page shows.
    cards = kpis.reorder_cards(
        lambda key: translate(lang, key),
        sum(1 for r in rows if r["item_type"] == "filament"),
        sum(1 for r in rows if r["item_type"] != "filament"),
        len(under_way),
    )
    stale_day = format_date(day, lang) if day else None
    return render(
        request, "reorder.html", status_code=status, active_nav="reorder", rows=rows, cards=cards,
        under_way=under_way, filament_unknown=sorts is None, error=error, stale_day=stale_day,
        # p182: recorded filament stock says so at the number, as on the filament page (p63)
        as_of=translate(lang, "filament.as_of", day=stale_day) if stale_day else None,
    )


@router.get("/reorder")
async def reorder_page(request: Request, db: Session = Depends(get_db)):
    return await _page(request, db)


async def _act(request: Request, db: Session, work, flash_key: str, typed_keys: bool = False):
    form = await request.form()
    try:
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            item = work(tx, form)
    except FormTokenUsed:
        return duplicate_redirect(request, "/reorder")
    except (DomainError, FormTokenInvalid) as exc:
        return await _page(
            request, db, error=error_text(current_language(request), exc), status=error_status(exc),
            typed={key: str(form.get(key, "")) for key in ("item_type", "item_id", "packs", "supplier_id")}
            if typed_keys else None,
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
        raw_supplier = str(form.get("supplier_id", "")).strip()
        supplier_id = bounded_int(raw_supplier) if raw_supplier else None  # empty: the default (Spec 9.4)
        if raw_supplier and supplier_id is None:
            raise NotFoundError("reorder.supplier_not_found")
        mark = reorder.mark_ordered(tx, str(form.get("item_type", "")), item_id, packs, supplier_id)
        return next((m.label for m in reorder.open_marks(tx) if m.id == mark.id), "")

    return await _act(request, db, work, "reorder.marked", typed_keys=True)


@router.post("/reorder/marks/{mark_id:int}/received", dependencies=[Depends(verify_csrf)])
async def reorder_received(request: Request, mark_id: int, db: Session = Depends(get_db)):
    def work(tx, form):
        label = next((m.label for m in reorder.open_marks(tx) if m.id == mark_id), "")
        reorder.mark_received(tx, mark_id)
        return label

    return await _act(request, db, work, "reorder.arrived")
