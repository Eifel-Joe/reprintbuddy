"""Purchased parts: list, create, edit (Spec 5.3). Stock changes only through bookings."""

from __future__ import annotations

from collections import Counter
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import MAX_SUPPLIERS, PartInput, SupplierInput
from reprintbuddy.domain.errors import DomainError, ValidationError
from reprintbuddy.domain.status import STATES, PartStatus, part_status, summarize
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Part, PartSupplier
from reprintbuddy.web import kpis, listing
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.formatting import canonical_unit, fill_percent
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.forms import (
    duplicate_redirect,
    run_form_action,
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
# Each supplier sends these once, in the order of the form (Spec 5.3); the default is the index
# in `supplier_default`. With a single supplier it is the default without choosing.
SUPPLIER_FIELDS = ("supplier", "order_url", "pack_size", "min_order_packs", "lead_time_days")
BLANK_SUPPLIER = {"supplier": "", "order_url": "", "pack_size": "1", "min_order_packs": "1", "lead_time_days": "0"}

DEFAULTS = {
    "name": "", "unit": "pcs", "initial_stock": "0", "min_stock": "0", "safety_value": "0",
    "safety_unit": "days",
}


def _defaults() -> dict[str, object]:
    return {**DEFAULTS, "supplier_rows": [dict(BLANK_SUPPLIER)], "supplier_default": "0"}


def _values_of(db: Session, part: Part) -> dict[str, object]:
    rows = catalog.part_suppliers(db, part.id)
    return {
        "name": part.name, "unit": part.unit, "min_stock": str(part.min_stock),
        "safety_value": str(part.safety_value), "safety_unit": part.safety_unit,
        "supplier_rows": [
            {
                "supplier": row.name or "", "order_url": row.order_url or "", "pack_size": str(row.pack_size),
                "min_order_packs": str(row.min_order_packs), "lead_time_days": str(row.lead_time_days),
            }
            for row in rows
        ],
        "supplier_default": next((str(i) for i, row in enumerate(rows) if row.is_default), ""),
    }


def _supplier_rows(form) -> list[dict[str, str]]:
    """The suppliers as typed, one dict per supplier; a missing value is empty."""
    columns = {key: [str(value) for value in form.getlist(key)] for key in SUPPLIER_FIELDS}
    count = max(len(values) for values in columns.values())
    return [
        {key: columns[key][i] if i < len(columns[key]) else "" for key in SUPPLIER_FIELDS} for i in range(count)
    ]


def _default_index(form, rows: list[dict[str, str]]) -> str:
    """The chosen default as an index; a single supplier is the default (Spec 5.3)."""
    raw = str(form.get("supplier_default", "")).strip()
    if raw.isdigit() and int(raw) < len(rows):
        return raw
    return "0" if len(rows) == 1 else ""


def _typed(form) -> dict[str, object]:
    values: dict[str, object] = {k: str(form.get(k, "")) for k in (*TEXT_FIELDS, *NUMBER_FIELDS)}
    rows = _supplier_rows(form)
    values["supplier_rows"] = rows
    values["supplier_default"] = _default_index(form, rows)
    return values


def _change_rows(form, values: dict[str, object]) -> bool:
    """'Add supplier' and 'remove' sent without JavaScript: change the rows, save nothing.

    The form comes back with the change, as the parts of an article do (routes/articles.py);
    part_suppliers.js does the same in place. The last supplier stays, the eleventh is not added.
    """
    rows = values["supplier_rows"]
    assert isinstance(rows, list)
    if form.get("suppliers_add") is not None:
        if len(rows) < MAX_SUPPLIERS:
            rows.append(dict(BLANK_SUPPLIER))
        values["focus"] = "add"
        return True
    raw = form.get("suppliers_remove")
    if raw is None:
        return False
    text = str(raw).strip()
    if text.isdigit() and int(text) < len(rows) and len(rows) > 1:
        index = int(text)
        rows.pop(index)
        default = str(values.get("supplier_default", ""))
        if default.isdigit():
            chosen = int(default)
            # the removed default leaves none chosen: saving then asks for one (Spec 5.3)
            values["supplier_default"] = "" if chosen == index else str(chosen - 1 if chosen > index else chosen)
    values["focus"] = "add"
    return True


def _numbered(number: int | None, parse):
    """Run `parse`; with several suppliers an error names the supplier (1 for the first)."""
    try:
        return parse()
    except ValidationError as error:
        if number is not None:
            error.params["supplier"] = number
        raise


def _parse_supplier(row: dict[str, str], number: int | None, is_default: bool) -> SupplierInput:
    return _numbered(number, lambda: SupplierInput(
        name=parse_text(row["supplier"], field="parts.field.supplier", max_len=200),
        order_url=parse_text(row["order_url"], field="parts.field.order_url", max_len=2000),
        pack_size=parse_int(row["pack_size"], field="parts.field.pack_size", min=1, max=MAX_QTY),
        min_order_packs=parse_int(row["min_order_packs"], field="parts.field.min_order_packs", min=1, max=1000),
        lead_time_days=parse_int(row["lead_time_days"], field="parts.field.lead_time_days", min=0, max=365),
        is_default=is_default,
    ))


def _parse(form) -> PartInput:
    rows = _supplier_rows(form)
    default = _default_index(form, rows)
    several = len(rows) > 1
    return PartInput(
        name=parse_text(form.get("name"), field="parts.field.name", max_len=200, required=True),
        unit=canonical_unit(parse_text(form.get("unit"), field="parts.field.unit", max_len=20, required=True)),
        min_stock=parse_int(form.get("min_stock"), field="parts.field.min_stock", min=0, max=MAX_QTY),
        safety_value=parse_int(form.get("safety_value"), field="parts.field.safety_value", min=0, max=MAX_QTY),
        safety_unit=str(form.get("safety_unit", "days")),
        suppliers=[
            _parse_supplier(row, i + 1 if several else None, str(i) == default) for i, row in enumerate(rows)
        ],
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
    states = STATES["part"]
    status = None if archived else listing.pick(request.query_params.get("status"), states)
    suppliers = catalog.suppliers_by_part(db)
    every = [_item(part, suppliers.get(part.id, [])) for part in catalog.list_parts(db, archived=archived)]
    found = [item for item in every if listing.matches(q, item.part.name)]
    counts = Counter(item.status for item in found)
    lang = current_language(request)
    t = lambda key: translate(lang, key)  # noqa: E731
    icon, label, named = ("archive", "kpi.parts_archived", []) if archived else (
        "puzzle", "kpi.parts", summarize((item.status for item in every), ("below_min",))
    )
    cards = kpis.state_cards(t, "part", icon, len(every), t(label), named)
    keep = (("status", status), ("view", "archived" if archived else None))
    layout = listing.layout_of(request, "parts")
    return render(
        request, "parts_list.html", active_nav="parts", archived=archived,
        items=[item for item in found if status is None or item.status == status],
        total=len(every),
        kpis=cards,
        filters=listing.filter_links(request, states, counts, len(found), status),
        layout=layout,
        layout_links=listing.layout_links(request, layout),
        q=q,
        search_keep=[(name, value) for name, value in keep if value],  # a new search keeps these
    )


@router.get("/parts/new")
async def parts_new(request: Request, db: Session = Depends(get_db)):
    return _form_page(request, db, part=None, values=_defaults())


@router.post("/parts/new", dependencies=[Depends(verify_csrf)])
async def parts_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    values = _typed(form)
    if _change_rows(form, values):
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


def _get_part(db: Session, part_id: int) -> Part:
    part = db.get(Part, part_id)
    if part is None:
        raise HTTPException(status_code=404)
    return part


@router.get("/parts/{part_id:int}/edit")
async def parts_edit(request: Request, part_id: int, db: Session = Depends(get_db)):
    part = _get_part(db, part_id)
    return _form_page(request, db, part=part, values=_values_of(db, part))


@router.post("/parts/{part_id:int}/edit", dependencies=[Depends(verify_csrf)])
async def parts_update(request: Request, part_id: int, db: Session = Depends(get_db)):
    part = _get_part(db, part_id)
    form = await request.form()
    values = _typed(form)
    if _change_rows(form, values):
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


def _archive_action(action, flash_key: str):
    async def handler(request: Request, part_id: int, db: Session = Depends(get_db)):
        part = _get_part(db, part_id)

        def work(tx):
            action(tx, part_id)
            return {"name": part.name}

        return await run_form_action(
            request,
            work=work,
            success_url="/parts",  # back to the list; an archived part leaves it, that is right
            flash_key=flash_key,
            on_error=lambda text, status, exc: _form_page(
                request, db, part=part, values=_values_of(db, part), error=text, status=status
            ),
        )

    return handler


router.add_api_route(
    "/parts/{part_id:int}/archive", _archive_action(catalog.archive_part, "parts.flash_archived"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)
router.add_api_route(
    "/parts/{part_id:int}/restore", _archive_action(catalog.restore_part, "parts.flash_restored"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)
