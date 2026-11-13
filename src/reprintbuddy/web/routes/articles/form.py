"""Shared helpers of the article pages: form fields, purchased parts, Bambuddy access (Spec 5.1, 5.2)."""

from __future__ import annotations

import re
from itertools import zip_longest

from fastapi import Request
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected
from reprintbuddy.bambuddy.types import Printer
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import MAX_LABEL_TEXT, ArticleInput, TargetOption
from reprintbuddy.domain.errors import DomainError, NotFoundError, ValidationError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Part
from reprintbuddy.web.deps import render
from reprintbuddy.web.forms import bounded_int, error_text, parse_bool, parse_int, parse_text
from reprintbuddy.web.formtoken import FormTokenInvalid
from reprintbuddy.web.filament_view import BAMBUDDY_ERRORS
from reprintbuddy.web.i18n import translate


# p153: appended to the address of the filament page when a person is led there right after creating
# an article or changing its print file; the page then offers "Later" instead of "Cancel".
LATER = "?later=1"
# p156: only right after creating: the page also starts from the group of a similar article (a display
# only: nothing is stored before "Save"). Not after a change of the file, not on a later visit.
NEW_ARTICLE = "?later=1&suggest=1"


class NotSliced(Exception):
    """The chosen print file is not sliced: it cannot become an article (Task 11)."""


# ---- shared form helpers (also used by the edit page) ----------------------------------


def target_choices(lang: str, options: list[TargetOption], selected: str) -> list[dict[str, object]]:
    """Option list for the print-target select, labelled in the interface language."""
    choices = []
    for option in options:
        if option.value == "":
            label = translate(lang, "articles.target_none")
        elif option.value.startswith("model:"):
            label = translate(lang, "articles.target_any", model=option.model)
        elif option.printer_name is not None:
            label = translate(lang, "articles.target_printer", name=option.printer_name, model=option.model)
        else:
            label = translate(lang, "articles.target_printer_id", id=option.value.split(":", 1)[1])
        choices.append({"value": option.value, "label": label, "selected": option.value == selected})
    return choices


# ---- purchased parts in the form (Spec 5.2) ---------------------------------------------------

MAX_PART_QTY = 1000
MAX_PART_ROWS = 200  # more rows than any article has parts; a forged form cannot make it grow further
_ROW_INDEX = re.compile(r"[0-9]{1,3}")


def _all_part_rows(form) -> list[dict[str, str]]:
    pairs = zip_longest(form.getlist("parts_part"), form.getlist("parts_qty"), fillvalue="")
    return [{"part": str(part).strip(), "qty": str(qty).strip()} for part, qty in pairs]


def part_rows(form) -> list[dict[str, str]]:
    """The rows of the parts list as typed, to show the form again: at most MAX_PART_ROWS."""
    return _all_part_rows(form)[:MAX_PART_ROWS]


def parse_parts(form, parts: list[Part]) -> dict[int, int]:
    """The parts in use (part id -> quantity per unit); none while the box is not ticked.

    A row without a part is ignored. Each part may be chosen once, with a quantity of at least 1;
    whether it exists and is active is for the catalog to say. Raises ValidationError or NotFoundError."""
    if not parse_bool(form.get("has_parts")):
        return {}
    names = {str(part.id): part.name for part in parts}
    chosen: dict[int, int] = {}
    rows = _all_part_rows(form)
    if len(rows) > MAX_PART_ROWS:  # not cut off: a part in a later row would be lost quietly
        raise ValidationError("articles.part_too_many", max=MAX_PART_ROWS)
    for row in rows:
        if not row["part"]:
            continue
        part_id = bounded_int(row["part"])
        if part_id is None:
            raise NotFoundError("parts.not_found")
        # A part the form does not offer (gone, or archived after the page was opened) goes on to
        # the catalog, which knows which of the two it is and says so.
        name = names.get(row["part"], row["part"])
        if part_id in chosen:
            raise ValidationError("articles.part_twice", name=name)
        try:
            chosen[part_id] = parse_int(row["qty"], field="articles.field.qty_per_unit", min=1, max=MAX_PART_QTY)
        except ValidationError:
            raise ValidationError("articles.part_qty_invalid", name=name, min=1, max=MAX_PART_QTY) from None
    return chosen


def change_part_rows(form, values: dict[str, object]) -> bool:
    """Adding or removing a row without JavaScript: True when the form is only to be shown again.

    Nothing is saved then and the one-time token is not used up. The new row, or else the add
    button, gets the focus, so the page opens where the user was."""
    rows = values["part_rows"]
    assert isinstance(rows, list)
    if form.get("parts_add") is not None:
        if len(rows) < MAX_PART_ROWS:
            rows.append({"part": "", "qty": ""})
        values["has_parts"] = True  # adding a part means parts are needed
        values["focus"] = len(rows) - 1
        return True
    raw = form.get("parts_remove")
    if raw is None:
        return False
    if _ROW_INDEX.fullmatch(str(raw)) and int(str(raw)) < len(rows):
        del rows[int(str(raw))]
    values["focus"] = "add"
    return True


def parse_article_form(form, parts: list[Part], *, default_target: str) -> ArticleInput:
    """Read the article settings from a form; raises ValidationError for bad input."""
    raw_target = form.get("target")
    target_model, printer_id = catalog.parse_target(default_target if raw_target is None else str(raw_target))
    return ArticleInput(
        name=parse_text(form.get("name"), field="articles.field.name", max_len=200, required=True),
        sku=parse_text(form.get("sku"), field="catalog.field.sku", max_len=64),
        label_text=parse_text(form.get("label_text"), field="articles.field.label_text", max_len=MAX_LABEL_TEXT),
        units_per_print=parse_int(form.get("units_per_print"), field="articles.field.units_per_print", min=1, max=1000),
        min_stock=parse_int(form.get("min_stock"), field="articles.field.min_stock", min=0, max=MAX_QTY),
        buffer=parse_int(form.get("buffer"), field="articles.field.buffer", min=0, max=MAX_QTY),
        target_model=target_model,
        printer_id=printer_id,
        needs_assembly=parse_bool(form.get("needs_assembly")),
        auto_enabled=parse_bool(form.get("auto_enabled")),
        parts=parse_parts(form, parts),
        sold_alone=parse_bool(form.get("sold_alone")),
    )


def typed_values(form) -> dict[str, object]:
    """What was typed, for showing the form again after an error."""
    return {
        "name": str(form.get("name", "")),
        "sku": str(form.get("sku", "")),
        "label_text": str(form.get("label_text", "")),
        "units_per_print": str(form.get("units_per_print", "")),
        "min_stock": str(form.get("min_stock", "")),
        "buffer": str(form.get("buffer", "")),
        "target": str(form.get("target", "")),
        "needs_assembly": parse_bool(form.get("needs_assembly")),
        "auto_enabled": parse_bool(form.get("auto_enabled")),
        "sold_alone": parse_bool(form.get("sold_alone")),
        "has_parts": parse_bool(form.get("has_parts")),
        "part_rows": part_rows(form),
        "focus": None,
    }


def values_of_input(data: ArticleInput, parts: list[Part]) -> dict[str, object]:
    in_use = [part for part in parts if part.id in data.parts]  # in the order of the offered parts
    return {
        "name": data.name,
        "sku": data.sku or "",
        "label_text": data.label_text or "",
        "units_per_print": str(data.units_per_print),
        "min_stock": str(data.min_stock),
        "buffer": str(data.buffer),
        "target": catalog.target_value(data.target_model, data.printer_id),
        "needs_assembly": data.needs_assembly,
        "auto_enabled": data.auto_enabled,
        "sold_alone": data.sold_alone,
        "has_parts": bool(in_use),
        "part_rows": [{"part": str(part.id), "qty": str(data.parts[part.id])} for part in in_use],
        "focus": None,
    }


# The errors that belong to one field of the form: shown under the field, not in the alert on top.
FIELD_ERRORS = {"articles.field_auto_needs_target": "auto_enabled"}


def form_error(lang: str, exc: DomainError | FormTokenInvalid) -> tuple[str | None, dict[str, str]]:
    """(text for the alert on top, texts by field) of a rejected form."""
    text = error_text(lang, exc)
    field = FIELD_ERRORS.get(exc.key) if isinstance(exc, DomainError) else None
    return (None, {field: text}) if field else (text, {})


def active_parts(db: Session) -> list[Part]:
    return catalog.list_parts(db)


# ---- Bambuddy access ---------------------------------------------------------------------


def _problem(request: Request, key: str, status: int = 200, *, relink: bool = False):
    """The error page of the file choice; `relink` gives it the subtitle of the relink path."""
    return render(
        request, "article_browse.html", status_code=status, active_nav="articles", problem=key, relink=relink
    )


async def _printers(client: BambuddyClient) -> list[Printer] | None:
    try:
        return await client.list_printers()
    except BAMBUDDY_ERRORS:
        return None


async def _read_plate(client: BambuddyClient, file_id: int, index: int):
    """File, plate, filament need and preview, read fresh from Bambuddy; None when gone."""
    file = await client.get_file(file_id)
    if file is None:
        return None
    if not file.sliced:  # before any plate, filament or preview request
        raise NotSliced
    plate = next((p for p in await client.list_plates(file_id) if p.index == index), None)
    if plate is None:
        return None
    requirements = await client.filament_requirements(file_id, index)
    try:
        thumbnail: bytes | None = await client.plate_thumbnail(file_id, index)
    except BambuddyRejected:
        thumbnail = None
    return file, plate, requirements, thumbnail
