"""Purchased parts: list, create, edit (Spec 5.3). Stock changes only through bookings."""

from __future__ import annotations

from typing import NamedTuple

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import MAX_SUPPLIERS, PartInput
from reprintbuddy.domain.errors import DomainError
from reprintbuddy.domain.suppliers import PART_LABELS
from reprintbuddy.domain.status import STATES, PartStatus, part_status, summarize
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Part, PartSupplier
from reprintbuddy.web import kpis, listing, supplier_form
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.archiving import add_archive_routes, to
from reprintbuddy.web.deps import current_language, get_db, get_or_404, render
from reprintbuddy.web.formatting import canonical_unit, fill_percent
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.forms import (
    duplicate_redirect,
    error_status,
    error_text,
    parse_int,
    parse_text,
    saved_redirect,
)
from reprintbuddy.web.formtoken import (
    FormTokenInvalid,
    FormTokenUsed,
    form_transaction,
    write_transaction,
)

router = APIRouter()

TEXT_FIELDS = ("name", "unit", "safety_unit")
NUMBER_FIELDS = ("initial_stock", "min_stock", "safety_value")
PACK_SIZE = 1  # a new supplier of a part starts with packs of one (Spec 5.3)

DEFAULTS = {
    "name": "", "unit": "pcs", "initial_stock": "0", "min_stock": "0", "safety_value": "0",
    "safety_unit": "days",
}


def _defaults() -> dict[str, object]:
    return {**DEFAULTS, "supplier_rows": [supplier_form.blank(PACK_SIZE)], "supplier_default": "0"}


def _values_of(db: Session, part: Part) -> dict[str, object]:
    return {
        "name": part.name, "unit": part.unit, "min_stock": str(part.min_stock),
        "safety_value": str(part.safety_value), "safety_unit": part.safety_unit,
        **supplier_form.values_of(catalog.part_suppliers(db, part.id)),
    }


def _typed(form) -> dict[str, object]:
    values: dict[str, object] = {k: str(form.get(k, "")) for k in (*TEXT_FIELDS, *NUMBER_FIELDS)}
    return {**values, **supplier_form.typed(form)}


def _parse(form) -> PartInput:
    return PartInput(
        name=parse_text(form.get("name"), field="parts.field.name", max_len=200, required=True),
        unit=canonical_unit(parse_text(form.get("unit"), field="parts.field.unit", max_len=20, required=True)),
        min_stock=parse_int(form.get("min_stock"), field="parts.field.min_stock", min=0, max=MAX_QTY),
        safety_value=parse_int(form.get("safety_value"), field="parts.field.safety_value", min=0, max=MAX_QTY),
        safety_unit=str(form.get("safety_unit", "days")),
        suppliers=supplier_form.parse(form, PART_LABELS),
    )


def _form_page(
    request: Request, db: Session, *, part: Part | None, values: dict[str, object], error: str | None = None,
    status: int = 200,
):
    return render(
        request,
        "part_form.html",
        status_code=status,
        active_nav="parts",
        part=part,
        values=values,
        error=error,
        supplier_names=catalog.supplier_names(db),
        max_suppliers=MAX_SUPPLIERS,
        supplier_texts=supplier_form.PART_TEXTS,
        supplier_blank=supplier_form.blank(PACK_SIZE),
    )


class PartItem(NamedTuple):
    """A part of the list with its state and how full it is towards its minimum stock."""

    part: Part
    status: PartStatus
    fill: int | None
    supplier: PartSupplier | None  # the default supplier (Spec 5.3)
    more: int  # how many suppliers besides the default


def _item(part: Part, suppliers: list[PartSupplier]) -> PartItem:
    default = next((row for row in suppliers if row.is_default), None)
    return PartItem(
        part,
        part_status(stock=part.stock, min_stock=part.min_stock),
        fill_percent(part.stock, part.min_stock),
        default,
        max(0, len(suppliers) - 1),
    )


@router.get("/parts")
async def parts_list(
    request: Request, view: str = "active", q: str | None = None, db: Session = Depends(get_db)
):
    """Overview with key figure cards, search, status filter and tiles or list (Spec 10.1).

    Same rules as the article list: the cards count all parts of the view, the filter counts
    follow the search, the list shows what passes both. The archive has no status filter.
    """
    if (redirect := listing.remember_layout(request, "parts")) is not None:
        return redirect  # ?layout=... only sets the cookie
    archived = view == "archived"
    suppliers = catalog.suppliers_by_part(db)
    every = [_item(part, suppliers.get(part.id, [])) for part in catalog.list_parts(db, archived=archived)]
    lang = current_language(request)
    t = lambda key: translate(lang, key)  # noqa: E731
    icon, label, named = ("archive", "kpi.parts_archived", []) if archived else (
        "puzzle", "kpi.parts", summarize((item.status for item in every), ("below_min",))
    )
    page = listing.list_page(
        request, page="parts", rows=every, states=STATES["part"], status_of=lambda item: item.status,
        search_texts=lambda item: (item.part.name,), q=q, status_filter=not archived,
        view="archived" if archived else None,
        cards=kpis.state_cards(t, "part", icon, len(every), t(label), named),
    )
    return render(
        request, "parts_list.html", active_nav="parts", archived=archived, items=page.shown, **page.context
    )


@router.get("/parts/new")
async def parts_new(request: Request, db: Session = Depends(get_db)):
    return _form_page(request, db, part=None, values=_defaults())


@router.post("/parts/new", dependencies=[Depends(verify_csrf)])
async def parts_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    values = _typed(form)
    if supplier_form.change_rows(form, values, PACK_SIZE):
        return _form_page(request, db, part=None, values=values)
    try:
        data = _parse(form)
        initial = parse_int(
            form.get("initial_stock"), field="parts.field.initial_stock", min=0, max=MAX_QTY
        )
        with form_transaction(request, str(form.get("form_token", ""))) as db:
            catalog.create_part(db, data, initial, "user")
    except FormTokenUsed:
        return duplicate_redirect(request, "/parts")
    except (DomainError, FormTokenInvalid) as exc:
        return _form_page(
            request, db, part=None, values=values,
            error=error_text(current_language(request), exc), status=error_status(exc),
        )
    return saved_redirect(request, "/parts", "parts.created", name=data.name)


@router.get("/parts/{part_id:int}/edit")
async def parts_edit(request: Request, part_id: int, db: Session = Depends(get_db)):
    part = get_or_404(db, Part, part_id)
    return _form_page(request, db, part=part, values=_values_of(db, part))


@router.post("/parts/{part_id:int}/edit", dependencies=[Depends(verify_csrf)])
async def parts_update(request: Request, part_id: int, db: Session = Depends(get_db)):
    part = get_or_404(db, Part, part_id)
    form = await request.form()
    values = _typed(form)
    if supplier_form.change_rows(form, values, PACK_SIZE):
        return _form_page(request, db, part=part, values=values)
    try:
        data = _parse(form)
        with write_transaction(request) as tx:
            catalog.update_part(tx, part_id, data)
    except DomainError as exc:
        return _form_page(
            request, db, part=part, values=values,
            error=error_text(current_language(request), exc), status=error_status(exc),
        )
    return saved_redirect(request, "/parts", "parts.updated", name=data.name)


add_archive_routes(
    router, base="/parts", param="part_id", load=lambda db, ident: get_or_404(db, Part, ident),
    archive=catalog.archive_part, restore=catalog.restore_part,
    flash_archived="parts.flash_archived", flash_restored="parts.flash_restored",
    success_url=to("/parts"),  # back to the list; an archived part leaves it, that is right
    on_error=lambda request, db, part, text, status, exc: _form_page(
        request, db, part=part, values=_values_of(db, part), error=text, status=status
    ),
)
