"""The filament page and the settings of a filament sort (Spec 9.3, 9.4, 10.3)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import filament, reorder, suppliers
from reprintbuddy.domain.errors import DomainError
from reprintbuddy.domain.filament import DEFAULT_ROLL_GRAMS, FilamentSettingsInput
from reprintbuddy.models import FilamentSettings, FilamentSort, utcnow
from reprintbuddy.web import kpis, supplier_form
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, db_session, get_db, get_or_404, render
from reprintbuddy.web.formatting import format_date
from reprintbuddy.web.forms import error_status, error_text, parse_int, saved_redirect
from reprintbuddy.web.filament_view import COLOR, may_write, page_spools
from reprintbuddy.web.formtoken import write_transaction
from reprintbuddy.web.i18n import translate

router = APIRouter()

# The safety margin stays at the sort; roll size, minimum order, lead time, shop and link sit at
# each supplier (Spec 9.3, p281), in the same block as a part's suppliers (web/supplier_form.py).
DEFAULTS = {"safety_value": "0", "safety_unit": "days"}


def _defaults() -> dict[str, object]:
    return {**DEFAULTS, "supplier_rows": [supplier_form.blank(DEFAULT_ROLL_GRAMS)], "supplier_default": "0"}


@router.get("/filament")
async def filament_list(request: Request):
    spools = await page_spools(request)  # before any write transaction
    lang = current_language(request)
    now = utcnow()
    today = filament.record_day(now)  # the server's day, not the browser's (Spec 9.3, p719)
    db = db_session(request)  # the rest only reads, on the request's normal session
    # The stock and the suggestions with the reorder page's function (p64): a sort with a suggestion is
    # "low" (Spec 9.4, 0.4.1), the sorts the overview's card "Filament" counts. It writes what the
    # reorder page writes (new sorts, Entscheidung 4; arrived marks closed).
    if not may_write(request):  # p230: a restore or an update runs, the page reads the record only
        state = reorder.current(db, None, now, write=False)
    else:
        with write_transaction(request) as tx:
            state = reorder.current(tx, spools, now)
    sorts, day = state.sorts, state.day
    short = {sug.item_id for sug in state.suggestions if sug.item_type == "filament"}
    ticked = filament.ticked_sort_ids(db)  # only spool sorts ticked in an active article (Spec 9.3)
    reordered = set(filament.reorder_groups(db))
    directory = [s for s in db.scalars(select(FilamentSort)) if s.id in ticked and not s.legacy]
    configured = set(db.scalars(select(FilamentSettings.spool_filament_id)))
    demand = reorder.filament_demand(db)
    # every view of the column "Verbrauch" in one query; the page shows Ø 30 days, the script the choice
    usage = filament.usage(db, [sort.id for sort in directory], today)
    rows = []
    for sort in directory:
        entry = sorts.get(sort.id) if sorts is not None else None
        is_reordered = sort.id in reordered
        rows.append({
            "id": sort.id,
            "label": sort.label,
            "material": sort.material or "",
            "swatch": sort.color_hex if sort.color_hex and COLOR.fullmatch(sort.color_hex) else None,
            "stock": f"{entry.stock_g:.0f}" if entry is not None else None,
            "spools": entry.spools if entry is not None else None,
            "usage": {view: usage[view].get(sort.id) for view in filament.USAGE_VIEWS},  # `num` filter
            "demand": f"{demand.get(sort.id, 0.0):.0f}" if is_reordered else None,
            "reordered": is_reordered,
            "configured": sort.id in configured,
            "short": sort.id in short,
        })
    unmapped = filament.unmapped_slots(db)
    rows.sort(key=lambda r: (r["label"].casefold(), r["id"]))
    stale_day = format_date(day, lang) if day else None
    as_of = translate(lang, "filament.as_of", day=stale_day) if stale_day else None
    # The key figures add up the numbers the rows show (the rounded grams), nothing is recomputed.
    # Unknown stock (sorts is None) leaves the stock and spool cards out instead of showing 0.
    known = sorts is not None
    cards = kpis.filament_cards(
        lambda key: translate(lang, key), len(rows),
        sum(int(r["stock"]) for r in rows if r["stock"] is not None) if known else None,
        sum(r["spools"] for r in rows if r["spools"] is not None) if known else None,
        sum(1 for r in rows if r["reordered"] and not r["configured"]),  # only these get suggestions
        # Recorded numbers say from when they are, at the numbers and not only in the notice on top (p63).
        recorded_note=as_of if as_of else "",
    )
    return render(
        request, "filament_list.html", active_nav="filament", rows=rows, unmapped=unmapped, cards=cards,
        live=sorts is not None and day is None, stale_day=stale_day, as_of=as_of,
        usage_views=_usage_views(lang, rows, today, filament.first_record_day(db)),
    )


# The texts of the views (literal keys: the language files keep only texts the code names).
_VIEW_KEYS = {
    "avg30": "filament.usage.view.avg30", "avg7": "filament.usage.view.avg7",
    "today": "filament.usage.view.today", "yesterday": "filament.usage.view.yesterday",
    "week": "filament.usage.view.week", "month": "filament.usage.view.month",
    "year": "filament.usage.view.year", "total": "filament.usage.view.total",
}
_MONTH_KEYS = (
    "date.month.1", "date.month.2", "date.month.3", "date.month.4", "date.month.5", "date.month.6",
    "date.month.7", "date.month.8", "date.month.9", "date.month.10", "date.month.11", "date.month.12",
)


def _usage_views(lang: str, rows: list[dict[str, object]], today: date, first_day: date | None) -> list[dict[str, object]]:
    """The views of the column "Verbrauch" for the head and the switch (Spec 9.3, p717).

    The calendar periods are named so that nobody takes them for the last n days: "diese Woche (KW 47)"
    (ISO week, which may begin in the year before), "dieser Monat (November)" in the language of the
    page, "dieses Jahr (2026)", and "gesamt (seit 14.11.2026)" with the first day of the record. All of
    them are days of the record in the zone of the server (Spec 9.3, 10.5a, p719), like `today`.
    An average adds over how many days, while the record is shorter than its window ("Ø 30 Tage,
    bisher 6"). Those days are the longest of the rows shown: a sort recorded for fewer days says its own
    days in its cell, so the head never claims more than a row has.
    """
    params: dict[str, dict[str, object]] = {
        "week": {"week": today.isocalendar().week},
        "month": {"month": translate(lang, _MONTH_KEYS[today.month - 1])},
        "year": {"year": today.year},
    }
    views = []
    for view in filament.USAGE_VIEWS:
        if view == "total":
            name = (translate(lang, "filament.usage.view.total", day=format_date(first_day, lang)) if first_day
                    else translate(lang, "filament.usage.view.total_none"))
        else:
            name = translate(lang, _VIEW_KEYS[view], **params.get(view, {}))
        window = filament.USAGE_WINDOW.get(view)
        days = max((u.days for r in rows if (u := r["usage"][view]) is not None), default=0)
        note = translate(lang, "filament.usage.so_far", view=name, days=days) if window and 0 < days < window else name
        views.append({
            "view": view, "average": window is not None, "name": name, "note": note, "days": days,
            "title": translate(lang, "filament.col.per_day" if window else "filament.usage.head_sum"),
        })
    return views


def _settings_page(request: Request, sort: FilamentSort, values: dict[str, object],
                   error: str | None = None, status: int = 200):
    return render(
        request, "filament_settings.html", status_code=status, active_nav="filament",
        sort_id=sort.id, label=sort.label, values=values, error=error,
        supplier_names=suppliers.names_in_use(db_session(request)), max_suppliers=suppliers.MAX_SUPPLIERS,
        supplier_texts=supplier_form.FILAMENT_TEXTS, supplier_blank=supplier_form.blank(DEFAULT_ROLL_GRAMS),
    )


def _get_sort(db: Session, sort_id: int) -> FilamentSort:
    # a sort of 0006 waits for its conversion and has no page
    return get_or_404(db, FilamentSort, sort_id, valid=lambda s: not s.legacy)


@router.get("/filament/{sort_id:int}")
async def filament_settings(request: Request, sort_id: int, db: Session = Depends(get_db)):
    sort = _get_sort(db, sort_id)
    row = filament.get_settings(db, sort_id)
    values = _defaults() if row is None else {
        "safety_value": str(row.safety_value), "safety_unit": row.safety_unit,
        **supplier_form.values_of(filament.sort_suppliers(db, sort_id)),
    }
    return _settings_page(request, sort, values)


@router.post("/filament/{sort_id:int}", dependencies=[Depends(verify_csrf)])
async def filament_settings_save(request: Request, sort_id: int, db: Session = Depends(get_db)):
    sort = _get_sort(db, sort_id)
    form = await request.form()
    values: dict[str, object] = {k: str(form.get(k, "")) for k in DEFAULTS}
    values.update(supplier_form.typed(form))
    if supplier_form.change_rows(form, values, DEFAULT_ROLL_GRAMS):
        return _settings_page(request, sort, values)
    try:
        data = FilamentSettingsInput(
            safety_value=parse_int(form.get("safety_value"), field="filament.field.safety_value", min=0, max=1_000_000),
            safety_unit=str(form.get("safety_unit", "days")),
            suppliers=supplier_form.parse(form, suppliers.FILAMENT_LABELS),
        )
        with write_transaction(request) as tx:
            filament.save_settings(tx, sort_id, data)
    except DomainError as exc:
        return _settings_page(request, sort, values, error_text(current_language(request), exc), error_status(exc))
    return saved_redirect(request, "/filament", "filament.settings_saved", name=sort.label)
