"""The filament page and the settings of a filament sort (Spec 9.3, 9.4, 10.3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import filament, reorder
from reprintbuddy.domain.errors import DomainError
from reprintbuddy.domain.filament import FilamentSettingsInput
from reprintbuddy.models import FilamentSettings, FilamentSort, utcnow
from reprintbuddy.web import kpis
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, db_session, get_db, render
from reprintbuddy.web.formatting import format_date
from reprintbuddy.web.forms import error_status, error_text, parse_int, parse_text, saved_redirect
from reprintbuddy.web.filament_view import COLOR, spools_or_none
from reprintbuddy.web.formtoken import write_transaction
from reprintbuddy.web.i18n import translate

router = APIRouter()

TEXT_FIELDS = ("supplier", "order_url", "safety_unit")
NUMBER_FIELDS = ("lead_time_days", "safety_value", "pack_grams", "min_order_packs")
DEFAULTS = {
    "lead_time_days": "0", "safety_value": "0", "safety_unit": "days", "pack_grams": "1000",
    "min_order_packs": "1", "supplier": "", "order_url": "",
}


@router.get("/filament")
async def filament_list(request: Request):
    spools = await spools_or_none(request)  # before any write transaction
    lang = current_language(request)
    today = utcnow().date()
    with write_transaction(request) as tx:  # entering new sorts is the one write (Entscheidung 4)
        sorts, day = filament.known_sorts(tx, spools)
    db = db_session(request)  # the rest only reads, on the request's normal session
    directory = db.scalars(select(FilamentSort)).all()
    configured = set(db.scalars(select(FilamentSettings.spool_filament_id)))
    demand = reorder.filament_demand(db)
    rows = []
    for sort in directory:
        entry = sorts.get(sort.id) if sorts is not None else None
        per_day, days = filament.consumption(db, sort.id, today)
        rows.append({
            "id": sort.id,
            "label": sort.label,
            "material": sort.material or "",
            "swatch": sort.color_hex if sort.color_hex and COLOR.fullmatch(sort.color_hex) else None,
            "stock": f"{entry.stock_g:.0f}" if entry is not None else None,
            "spools": entry.spools if entry is not None else None,
            "per_day": f"{per_day:.1f}" if days else None,
            "demand": f"{demand.get(sort.id, 0.0):.0f}",
            "configured": sort.id in configured,
        })
    unmapped = filament.unmapped_slots(db)
    rows.sort(key=lambda r: (r["label"].casefold(), r["id"]))
    # The key figures add up the numbers the rows show (the rounded grams), nothing is recomputed.
    # Unknown stock (sorts is None) leaves the stock and spool cards out instead of showing 0.
    known = sorts is not None
    cards = kpis.filament_cards(
        lambda key: translate(lang, key), len(rows),
        sum(int(r["stock"]) for r in rows if r["stock"] is not None) if known else None,
        sum(r["spools"] for r in rows if r["spools"] is not None) if known else None,
        sum(1 for r in rows if not r["configured"]),
    )
    return render(
        request, "filament_list.html", active_nav="filament", rows=rows, unmapped=unmapped, cards=cards,
        live=sorts is not None and day is None, stale_day=format_date(day, lang) if day else None,
    )


def _settings_page(request: Request, sort: FilamentSort, values: dict[str, str],
                   error: str | None = None, status: int = 200):
    return render(
        request, "filament_settings.html", status_code=status, active_nav="filament",
        sort_id=sort.id, label=sort.label, values=values, error=error,
    )


def _get_sort(db: Session, sort_id: int) -> FilamentSort:
    sort = db.get(FilamentSort, sort_id)
    if sort is None:
        raise HTTPException(status_code=404)
    return sort


@router.get("/filament/{sort_id:int}")
async def filament_settings(request: Request, sort_id: int, db: Session = Depends(get_db)):
    sort = _get_sort(db, sort_id)
    row = filament.get_settings(db, sort_id)
    values = dict(DEFAULTS) if row is None else {
        "lead_time_days": str(row.lead_time_days), "safety_value": str(row.safety_value),
        "safety_unit": row.safety_unit, "pack_grams": str(row.pack_grams),
        "min_order_packs": str(row.min_order_packs), "supplier": row.supplier or "",
        "order_url": row.order_url or "",
    }
    return _settings_page(request, sort, values)


@router.post("/filament/{sort_id:int}", dependencies=[Depends(verify_csrf)])
async def filament_settings_save(request: Request, sort_id: int, db: Session = Depends(get_db)):
    sort = _get_sort(db, sort_id)
    form = await request.form()
    values = {k: str(form.get(k, "")) for k in (*TEXT_FIELDS, *NUMBER_FIELDS)}
    try:
        data = FilamentSettingsInput(
            lead_time_days=parse_int(form.get("lead_time_days"), field="filament.field.lead_time_days", min=0, max=365),
            safety_value=parse_int(form.get("safety_value"), field="filament.field.safety_value", min=0, max=1_000_000),
            safety_unit=str(form.get("safety_unit", "days")),
            pack_grams=parse_int(form.get("pack_grams"), field="filament.field.pack_grams", min=1, max=100_000),
            min_order_packs=parse_int(form.get("min_order_packs"), field="filament.field.min_order_packs", min=1, max=1000),
            supplier=parse_text(form.get("supplier"), field="filament.field.supplier", max_len=200),
            order_url=parse_text(form.get("order_url"), field="filament.field.order_url", max_len=2000),
        )
        with write_transaction(request) as tx:
            filament.save_settings(tx, sort_id, data)
    except DomainError as exc:
        return _settings_page(request, sort, values, error_text(current_language(request), exc), error_status(exc))
    return saved_redirect(request, "/filament", "filament.settings_saved", name=sort.label)
