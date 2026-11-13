"""Suppliers of a purchased part or a spool sort (Spec 5.3, 9.3): one way to check, save and compare.

Both kinds have the same fields per supplier (name, order link, pack size, minimum order, lead
time, default) and the same rules (1 to 10, exactly one default). Parts keep them in
`part_suppliers`, spool sorts in `filament_suppliers`; only the owner column and the labels of the
fields differ. Functions never commit; the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import inputs
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.models import FilamentSupplier, PartSupplier

MAX_SUPPLIERS = 10  # per part or spool sort (Spec 5.3, 9.3)
COLUMNS = ("name", "order_url", "pack_size", "min_order_packs", "lead_time_days", "is_default")
MAX_NAME = 200
MAX_URL = 2000


class SupplierRow(Protocol):
    id: int
    position: int
    name: str | None
    order_url: str | None
    pack_size: int
    min_order_packs: int
    lead_time_days: int
    is_default: bool


@dataclass
class SupplierInput:
    """One supplier as a form sends it: where it is bought, in which packs, how fast."""

    name: str | None = None
    order_url: str | None = None
    pack_size: int = 1
    min_order_packs: int = 1
    lead_time_days: int = 0
    is_default: bool = False


@dataclass(frozen=True)
class Labels:
    """The message keys of one kind's form: the field names an error names, and the two rules."""

    name: str
    order_url: str
    invalid_url: str
    pack_size: str
    pack_max: int
    min_order_packs: str
    lead_time_days: str
    count: str
    default: str


PART_LABELS = Labels(
    name="parts.field.supplier_name", order_url="parts.field.order_url", invalid_url="parts.invalid_url",
    pack_size="parts.field.pack_size", pack_max=1_000_000, min_order_packs="parts.field.min_order_packs",
    lead_time_days="parts.field.lead_time_days", count="parts.suppliers_count", default="parts.supplier_default",
)
FILAMENT_LABELS = Labels(
    name="filament.field.supplier_name", order_url="filament.field.order_url", invalid_url="filament.invalid_url",
    pack_size="filament.field.pack_grams", pack_max=100_000, min_order_packs="filament.field.min_order_packs",
    lead_time_days="filament.field.lead_time_days", count="filament.suppliers_count",
    default="filament.supplier_default",
)




def _check_one(data: SupplierInput, labels: Labels, number: int | None) -> SupplierInput:
    """One supplier's fields; with several, an error names the supplier by its number (1 first)."""
    try:
        url = inputs.text(data.order_url, labels.order_url, MAX_URL)
        if url is not None and not url.lower().startswith(("http://", "https://")):
            raise ValidationError(labels.invalid_url)
        return SupplierInput(
            name=inputs.text(data.name, labels.name, MAX_NAME),
            order_url=url,
            pack_size=inputs.int_in(data.pack_size, labels.pack_size, 1, labels.pack_max),
            min_order_packs=inputs.int_in(data.min_order_packs, labels.min_order_packs, 1, 1000),
            lead_time_days=inputs.int_in(data.lead_time_days, labels.lead_time_days, 0, 365),
            is_default=bool(data.is_default),
        )
    except ValidationError as error:
        if number is not None:
            error.params["supplier"] = number
        raise


def check(items: Sequence[SupplierInput], labels: Labels) -> list[SupplierInput]:
    """1 to 10 suppliers, each within its limits, exactly one the default (Spec 5.3, 9.3)."""
    if not 1 <= len(items) <= MAX_SUPPLIERS:
        raise ValidationError(labels.count, max=MAX_SUPPLIERS)
    several = len(items) > 1
    checked = [_check_one(item, labels, number if several else None) for number, item in enumerate(items, start=1)]
    if sum(item.is_default for item in checked) != 1:
        raise ValidationError(labels.default)
    return checked


def apply(
    session: Session, rows: Sequence[SupplierRow], wanted: Sequence[SupplierInput], new_row: Callable[[int], object]
) -> None:
    """Make the stored suppliers (`rows`, in the order of the form) what the form says, compared by
    position; `new_row(position)` makes a row for a supplier added at the end.

    Unchanged rows are not touched, so saving the same values writes nothing: no log line, and no
    other open form is told it changed (Spec 10.5, 10.7). Rejected: deleting and writing every row
    again, which is no change for the log (`capture` compares child rows) but is one for the live
    feed. The default moves in two steps: the unique index allows one default per owner at any time.
    """
    for row in rows[len(wanted):]:
        session.delete(row)
    for row, item in zip(rows, wanted):
        if row.is_default and not item.is_default:
            row.is_default = False
    session.flush()
    for position, item in enumerate(wanted):
        row = rows[position] if position < len(rows) else None
        if row is None:
            row = new_row(position)
            session.add(row)
        for column in COLUMNS:
            if getattr(row, column) != getattr(item, column):
                setattr(row, column, getattr(item, column))
    session.flush()


def key(rows: Sequence[SupplierRow]) -> str:
    """A short fingerprint of the suppliers as a page shows them.

    Saving compares the rows by position, so removing a supplier moves the next one into its row
    and its id. A reorder page shown before such a change sends the key of what it showed; a mark
    with a key that no longer matches is refused rather than set on the wrong supplier.
    """
    seen = [
        (r.id, r.position, r.name, r.order_url, r.pack_size, r.min_order_packs, r.lead_time_days, bool(r.is_default))
        for r in rows
    ]
    return hashlib.sha256(json.dumps(seen).encode("utf-8")).hexdigest()[:16]


def names_in_use(session: Session) -> list[str]:
    """The supplier names in use at parts and spool sorts, offered while typing (Spec 9.3: the same
    shops often sell both); each once, sorted."""
    found: set[str] = set()
    for model in (PartSupplier, FilamentSupplier):
        found.update(session.scalars(select(model.name).where(model.name.is_not(None)).distinct()))
    return sorted(found, key=lambda name: (name.casefold(), name))
