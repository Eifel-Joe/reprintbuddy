"""Purchased parts of the catalog."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import inputs, suppliers
from reprintbuddy.domain.errors import NotFoundError, ValidationError
from reprintbuddy.domain.stock import Actor, MAX_QTY, count_part
from reprintbuddy.models import Part, PartSupplier
from reprintbuddy.domain.catalog.articles import _archive_filter, _required_name


# ---- purchased parts -----------------------------------------------------------------------


# The suppliers of a part (Spec 5.3) are checked, saved and compared like those of a spool sort.
MAX_SUPPLIERS = suppliers.MAX_SUPPLIERS
SupplierInput = suppliers.SupplierInput


def _one_default() -> list[SupplierInput]:
    return [SupplierInput(is_default=True)]


@dataclass
class PartInput:
    name: str
    unit: str = "pcs"
    min_stock: int = 0
    safety_value: int = 0
    safety_unit: Literal["days", "qty"] = "days"
    # in the order of the form; a part has 1 to 10, exactly one the default (Spec 5.3)
    suppliers: list[SupplierInput] = field(default_factory=_one_default)


def _check_part_input(data: PartInput) -> PartInput:
    unit = inputs.text(data.unit, "parts.field.unit", 20, required=True)
    if data.safety_unit not in ("days", "qty"):
        raise ValidationError("parts.invalid_safety_unit")
    checked = suppliers.check(data.suppliers, suppliers.PART_LABELS)
    return replace(
        data,
        name=_required_name(data.name, "parts.field.name"),
        unit=unit,
        min_stock=inputs.int_in(data.min_stock, "parts.field.min_stock", 0, MAX_QTY),
        safety_value=inputs.int_in(data.safety_value, "parts.field.safety_value", 0, MAX_QTY),
        suppliers=checked,
    )


def _apply_part(part: Part, data: PartInput) -> None:
    part.name = data.name
    part.unit = data.unit
    part.min_stock = data.min_stock
    part.safety_value = data.safety_value
    part.safety_unit = data.safety_unit


def _apply_suppliers(session: Session, part_id: int, wanted: list[SupplierInput]) -> None:
    """Make the part's suppliers what the form says, compared by position (`suppliers.apply`)."""
    suppliers.apply(
        session, part_suppliers(session, part_id), wanted,
        lambda position: PartSupplier(part_id=part_id, position=position),
    )


def part_suppliers(session: Session, part_id: int) -> list[PartSupplier]:
    """The suppliers of a part in the order of its form."""
    return list(
        session.scalars(
            select(PartSupplier).where(PartSupplier.part_id == part_id).order_by(PartSupplier.position)
        )
    )


suppliers_key = suppliers.key  # what a reorder page showed of a part's suppliers


def default_supplier(session: Session, part_id: int) -> PartSupplier | None:
    """The default supplier of a part (every part has one, Spec 5.3)."""
    return session.scalar(
        select(PartSupplier).where(PartSupplier.part_id == part_id, PartSupplier.is_default.is_(True))
    )


def suppliers_by_part(session: Session) -> dict[int, list[PartSupplier]]:
    """The suppliers of every part, in the order of their forms; one query for a list page."""
    found: dict[int, list[PartSupplier]] = {}
    for row in session.scalars(select(PartSupplier).order_by(PartSupplier.part_id, PartSupplier.position)):
        found.setdefault(row.part_id, []).append(row)
    return found


supplier_names = suppliers.names_in_use  # offered while typing, from parts and spool sorts (Spec 9.3)


def create_part(session: Session, data: PartInput, initial_stock: int, actor: Actor) -> Part:
    """New purchased part; the initial stock is booked as an inventory count (Spec 5.3, 6.5)."""
    data = _check_part_input(data)
    initial_stock = inputs.int_in(initial_stock, "parts.field.initial_stock", 0, MAX_QTY)
    part = Part()
    _apply_part(part, data)
    session.add(part)
    session.flush()
    _apply_suppliers(session, part.id, data.suppliers)
    if initial_stock > 0:
        count_part(session, part.id, initial_stock, actor)
    return part


def update_part(session: Session, part_id: int, data: PartInput) -> Part:
    """Change the settings and suppliers of a part; its stock only changes through bookings."""
    part = session.get(Part, part_id)
    if part is None:
        raise NotFoundError("parts.not_found")
    data = _check_part_input(data)
    _apply_part(part, data)
    session.flush()
    _apply_suppliers(session, part_id, data.suppliers)
    return part


def list_parts(session: Session, *, archived: bool = False) -> list[Part]:
    parts = session.scalars(select(Part).where(_archive_filter(Part.archived_at, archived))).all()
    return sorted(parts, key=lambda p: (p.name.casefold(), p.id))
