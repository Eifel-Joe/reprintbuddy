"""The block "Suppliers" of a form (Spec 5.3, 9.3): read it, add and remove without JavaScript.

The form of a purchased part and the order settings of a spool sort show the same block
(templates/_suppliers.html, static/js/suppliers.js). Each supplier sends the same field names once,
in the order of the form; the default is its index in `supplier_default`. With a single supplier
it is the default without choosing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.domain.suppliers import MAX_NAME, MAX_SUPPLIERS, MAX_URL, Labels, SupplierInput, SupplierRow
from reprintbuddy.web.forms import parse_int, parse_text

FIELDS = ("supplier", "order_url", "pack_size", "min_order_packs", "lead_time_days")


@dataclass(frozen=True)
class Texts:
    """The texts of one kind's block, as keys of the language files; the template shows them."""

    title: str
    hint: str
    name: str
    order_url: str
    pack_size: str
    min_order_packs: str
    lead_time_days: str
    info_pack_size: str
    info_min_order_packs: str
    info_lead_time_days: str


PART_TEXTS = Texts(
    title="parts.suppliers_title", hint="parts.suppliers_hint", name="parts.field.supplier_name",
    order_url="parts.field.order_url", pack_size="parts.field.pack_size",
    min_order_packs="parts.field.min_order_packs", lead_time_days="parts.field.lead_time_days",
    info_pack_size="info.pack_size", info_min_order_packs="info.min_order_packs",
    info_lead_time_days="info.lead_time_days",
)
FILAMENT_TEXTS = Texts(
    title="filament.suppliers_title", hint="filament.suppliers_hint", name="filament.field.supplier_name",
    order_url="filament.field.order_url", pack_size="filament.field.pack_grams",
    min_order_packs="filament.field.min_order_packs", lead_time_days="filament.field.lead_time_days",
    info_pack_size="info.filament_pack_grams", info_min_order_packs="info.filament_min_order_packs",
    info_lead_time_days="info.filament_lead_time",
)


def blank(pack_size: int) -> dict[str, str]:
    """A new, empty supplier as the form shows it; a part starts with packs of 1, a roll with 1000 g."""
    return {"supplier": "", "order_url": "", "pack_size": str(pack_size), "min_order_packs": "1", "lead_time_days": "0"}


def values_of(rows: Sequence[SupplierRow]) -> dict[str, object]:
    """The stored suppliers as the form shows them."""
    return {
        "supplier_rows": [
            {
                "supplier": row.name or "", "order_url": row.order_url or "", "pack_size": str(row.pack_size),
                "min_order_packs": str(row.min_order_packs), "lead_time_days": str(row.lead_time_days),
            }
            for row in rows
        ],
        "supplier_default": next((str(i) for i, row in enumerate(rows) if row.is_default), ""),
    }


def typed(form) -> dict[str, object]:
    """The suppliers as typed, one dict per supplier (a missing value is empty), and the default."""
    columns = {key: [str(value) for value in form.getlist(key)] for key in FIELDS}
    count = max(len(values) for values in columns.values())
    rows = [{key: columns[key][i] if i < len(columns[key]) else "" for key in FIELDS} for i in range(count)]
    return {"supplier_rows": rows, "supplier_default": _default_index(form, rows)}


def _default_index(form, rows: list[dict[str, str]]) -> str:
    raw = str(form.get("supplier_default", "")).strip()
    if raw.isdigit() and int(raw) < len(rows):
        return raw
    return "0" if len(rows) == 1 else ""


def change_rows(form, values: dict[str, object], pack_size: int) -> bool:
    """'Add supplier' and 'remove' sent without JavaScript: change the rows, save nothing.

    The form comes back with the change, as the parts of an article do (routes/articles.py);
    suppliers.js does the same in place. The last supplier stays, the eleventh is not added.
    """
    rows = values["supplier_rows"]
    assert isinstance(rows, list)
    if form.get("suppliers_add") is not None:
        if len(rows) < MAX_SUPPLIERS:
            rows.append(blank(pack_size))
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
            # the removed default leaves none chosen: saving then asks for one (Spec 5.3, 9.3)
            values["supplier_default"] = "" if chosen == index else str(chosen - 1 if chosen > index else chosen)
    values["focus"] = "add"
    return True


def parse(form, labels: Labels) -> list[SupplierInput]:
    """The suppliers of the form as input for the domain; with several, an error names the supplier."""
    rows = typed(form)["supplier_rows"]
    assert isinstance(rows, list)
    default = _default_index(form, rows)
    several = len(rows) > 1
    return [_parse_one(row, labels, i + 1 if several else None, str(i) == default) for i, row in enumerate(rows)]


def _parse_one(row: dict[str, str], labels: Labels, number: int | None, is_default: bool) -> SupplierInput:
    try:
        return SupplierInput(
            name=parse_text(row["supplier"], field=labels.name, max_len=MAX_NAME),
            order_url=parse_text(row["order_url"], field=labels.order_url, max_len=MAX_URL),
            pack_size=parse_int(row["pack_size"], field=labels.pack_size, min=1, max=labels.pack_max),
            min_order_packs=parse_int(row["min_order_packs"], field=labels.min_order_packs, min=1, max=1000),
            lead_time_days=parse_int(row["lead_time_days"], field=labels.lead_time_days, min=0, max=365),
            is_default=is_default,
        )
    except ValidationError as error:
        if number is not None:
            error.params["supplier"] = number
        raise
