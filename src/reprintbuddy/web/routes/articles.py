"""Articles: list and creation from a Bambuddy print file (Spec 5.1, 5.2).

Bambuddy is only read here, through `BambuddyClient`. When creating, the file, the
plates, the filament need and the preview are read again from Bambuddy on submit
and nothing is taken from hidden fields.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate, Printer
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import ArticleInput, TargetOption
from reprintbuddy.domain.errors import DomainError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Article, Part
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_bambuddy, get_db, render
from reprintbuddy.web.forms import (
    duplicate_redirect,
    error_status,
    error_text,
    parse_bool,
    parse_int,
    parse_text,
    saved_redirect,
)
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction
from reprintbuddy.web.i18n import translate

router = APIRouter()

BAMBUDDY_ERRORS = (BambuddyUnavailable, BambuddyRejected)


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


def parse_article_form(form, part_ids: list[int], *, default_target: str) -> ArticleInput:
    """Read the article settings from a form; raises ValidationError for bad input."""
    raw_target = form.get("target")
    target_model, printer_id = catalog.parse_target(default_target if raw_target is None else str(raw_target))
    parts: dict[int, int] = {}
    for part_id in part_ids:
        qty = parse_int(form.get(f"part_{part_id}"), field="articles.field.qty_per_unit", min=0, max=1000, default=0)
        if qty:
            parts[part_id] = qty
    return ArticleInput(
        name=parse_text(form.get("name"), field="articles.field.name", max_len=200, required=True),
        sku=parse_text(form.get("sku"), field="catalog.field.sku", max_len=64),
        units_per_print=parse_int(form.get("units_per_print"), field="articles.field.units_per_print", min=1, max=1000),
        min_stock=parse_int(form.get("min_stock"), field="articles.field.min_stock", min=0, max=MAX_QTY),
        buffer=parse_int(form.get("buffer"), field="articles.field.buffer", min=0, max=MAX_QTY),
        target_model=target_model,
        printer_id=printer_id,
        needs_assembly=parse_bool(form.get("needs_assembly")),
        auto_enabled=parse_bool(form.get("auto_enabled")),
        parts=parts,
    )


def typed_values(form, part_ids: list[int]) -> dict[str, object]:
    """What was typed, for showing the form again after an error."""
    return {
        "name": str(form.get("name", "")),
        "sku": str(form.get("sku", "")),
        "units_per_print": str(form.get("units_per_print", "")),
        "min_stock": str(form.get("min_stock", "")),
        "buffer": str(form.get("buffer", "")),
        "target": str(form.get("target", "")),
        "needs_assembly": parse_bool(form.get("needs_assembly")),
        "auto_enabled": parse_bool(form.get("auto_enabled")),
        "parts": {pid: str(form.get(f"part_{pid}", "")) for pid in part_ids},
    }


def values_of_input(data: ArticleInput, part_ids: list[int]) -> dict[str, object]:
    return {
        "name": data.name,
        "sku": data.sku or "",
        "units_per_print": str(data.units_per_print),
        "min_stock": str(data.min_stock),
        "buffer": str(data.buffer),
        "target": catalog.target_value(data.target_model, data.printer_id),
        "needs_assembly": data.needs_assembly,
        "auto_enabled": data.auto_enabled,
        "parts": {pid: str(data.parts.get(pid, 0)) for pid in part_ids},
    }


def active_parts(db: Session) -> list[Part]:
    return catalog.list_parts(db)


# ---- Bambuddy access ---------------------------------------------------------------------


def _problem(request: Request, key: str, status: int = 200):
    """The error page of the creation flow."""
    return render(request, "article_browse.html", status_code=status, active_nav="articles", problem=key)


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
    plate = next((p for p in await client.list_plates(file_id) if p.index == index), None)
    if plate is None:
        return None
    requirements = await client.filament_requirements(file_id, index)
    try:
        thumbnail: bytes | None = await client.plate_thumbnail(file_id, index)
    except BambuddyRejected:
        thumbnail = None
    return file, plate, requirements, thumbnail


# ---- list ----------------------------------------------------------------------------------


@router.get("/articles")
async def articles_list(request: Request, db: Session = Depends(get_db)):
    return render(request, "articles_list.html", active_nav="articles", rows=catalog.list_articles(db))


# ---- creation: browse folders and files, choose a plate ------------------------------------------


@router.get("/articles/new")
async def articles_new(request: Request, folder: str | None = None):
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing")
    try:
        folder_id = int(folder) if folder and folder.isdigit() else None
        folders = await client.list_folders()
        files = await client.list_files(folder_id)
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error")
    by_id = {f.id: f for f in folders}
    crumbs, seen, current = [], set(), by_id.get(folder_id) if folder_id is not None else None
    while current is not None and current.id not in seen:
        seen.add(current.id)
        crumbs.append(current)
        current = by_id.get(current.parent_id) if current.parent_id is not None else None
    crumbs.reverse()
    subfolders = sorted((f for f in folders if f.parent_id == folder_id), key=lambda f: f.name.casefold())
    return render(
        request,
        "article_browse.html",
        active_nav="articles",
        problem=None,
        crumbs=crumbs,
        up_id=crumbs[-2].id if len(crumbs) > 1 else None,
        in_folder=bool(crumbs),
        subfolders=subfolders,
        files=sorted(files, key=lambda f: f.filename.casefold()),
        base="/articles/new",
        file_base="/articles/new/file",
    )


def _taken_plates(db: Session, file_id: int) -> dict[int, str]:
    rows = db.execute(
        select(Article.plate_id, Article.name).where(
            Article.library_file_id == file_id, Article.archived_at.is_(None)
        )
    ).all()
    return {plate_id: name for plate_id, name in rows}


@router.get("/articles/new/file/{file_id:int}")
async def articles_plates(request: Request, file_id: int, db: Session = Depends(get_db)):
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing")
    try:
        file = await client.get_file(file_id)
        plates = await client.list_plates(file_id) if file is not None else []
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error")
    if file is None:
        return _problem(request, "articles.file_missing", 404)
    return render(
        request,
        "article_plates.html",
        active_nav="articles",
        file=file,
        plates=plates,
        taken=_taken_plates(db, file_id),
        file_base="/articles/new/file",
        back=f"/articles/new?folder={file.folder_id}" if file.folder_id is not None else "/articles/new",
    )


@router.get("/articles/new/file/{file_id:int}/plate/{index:int}/thumbnail")
async def articles_thumbnail(request: Request, file_id: int, index: int):
    client = get_bambuddy(request)
    if client is None:
        return Response(status_code=404)
    try:
        data = await client.plate_thumbnail(file_id, index)
    except BAMBUDDY_ERRORS:
        return Response(status_code=404)
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


_COLOR = re.compile(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?")


def _requirement_row(r: FilamentRequirement) -> dict[str, object]:
    """One row of the filament table; the colour is only shown as a swatch when it is a plain hex value."""
    return {
        "slot": r.slot,
        "material": r.material or "",
        "grams": f"{r.grams:.1f}",
        "color": r.color_hex or "",
        "swatch": r.color_hex if r.color_hex and _COLOR.fullmatch(r.color_hex) else None,
    }


# ---- creation: the form ----------------------------------------------------------------------


def _plate_form(
    request: Request,
    *,
    action: str,
    file: LibraryFile,
    plate: Plate,
    requirements: list[FilamentRequirement],
    printers: list[Printer] | None,
    parts: list[Part],
    values: Mapping[str, object],
    error: str | None = None,
    status: int = 200,
):
    lang = current_language(request)
    selected = str(values["target"])
    options = catalog.target_options(printers, file.printer_model, current=selected)
    return render(
        request,
        "article_form.html",
        status_code=status,
        active_nav="articles",
        action=action,
        file=file,
        plate=plate,
        requirements=[_requirement_row(r) for r in requirements],
        target_choices=target_choices(lang, options, selected),
        printers_unavailable=printers is None,
        parts=parts,
        values=values,
        error=error,
    )


@router.get("/articles/new/file/{file_id:int}/plate/{index:int}")
async def articles_plate_form(request: Request, file_id: int, index: int, db: Session = Depends(get_db)):
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing")
    try:
        read = await _read_plate(client, file_id, index)
        printers = await client.list_printers() if read is not None else None
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error")
    if read is None:
        return _problem(request, "articles.file_missing", 404)
    file, plate, requirements, _thumbnail = read
    parts = active_parts(db)
    values = values_of_input(catalog.default_article_input(file), [p.id for p in parts])
    return _plate_form(
        request, action=f"/articles/new/file/{file_id}/plate/{index}", file=file, plate=plate,
        requirements=requirements, printers=printers, parts=parts, values=values,
    )


@router.post("/articles/new/file/{file_id:int}/plate/{index:int}", dependencies=[Depends(verify_csrf)])
async def articles_create(request: Request, file_id: int, index: int, db: Session = Depends(get_db)):
    form = await request.form()
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing")
    try:  # everything from Bambuddy is read first, outside of the write transaction
        read = await _read_plate(client, file_id, index)
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error")
    if read is None:
        return _problem(request, "articles.file_missing")
    file, plate, requirements, thumbnail = read
    parts = active_parts(db)
    part_ids = [p.id for p in parts]
    action = f"/articles/new/file/{file_id}/plate/{index}"
    lang = current_language(request)
    try:
        data = parse_article_form(
            form, part_ids, default_target=catalog.target_value(file.printer_model, None)
        )
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            article = catalog.create_article(
                tx, file=file, plate=plate, requirements=requirements, thumbnail=thumbnail, data=data
            )
            article_id, article_name = article.id, article.name
    except FormTokenUsed:
        return duplicate_redirect(request, "/articles")
    except (DomainError, FormTokenInvalid) as exc:
        printers = await _printers(client)
        return _plate_form(
            request, action=action, file=file, plate=plate, requirements=requirements,
            printers=printers, parts=parts, values=typed_values(form, part_ids),
            error=error_text(lang, exc), status=error_status(exc),
        )
    return saved_redirect(request, f"/articles/{article_id}", "articles.created", name=article_name)
