"""Articles: list and creation from a Bambuddy print file (Spec 5.1, 5.2).

Bambuddy is only read here, through `BambuddyClient`. When creating, the file, the
plates, the filament need and the preview are read again from Bambuddy on submit
and nothing is taken from hidden fields.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate, Printer
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import ArticleInput, TargetOption
from reprintbuddy.domain.errors import DomainError
from reprintbuddy.domain.status import STATES, summarize
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.domain import stock as stock_mod
from reprintbuddy.models import Article, ArticleFilament, ArticlePart, Part
from reprintbuddy.web import kpis, listing
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_bambuddy, get_db, render
from reprintbuddy.web.forms import (
    bounded_int,
    duplicate_redirect,
    run_form_action,
    error_status,
    error_text,
    parse_bool,
    parse_int,
    parse_text,
    saved_redirect,
)
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction
from reprintbuddy.web.journal_view import journal_rows
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
async def articles_list(
    request: Request, view: str = "active", q: str | None = None, db: Session = Depends(get_db)
):
    """Overview with key figure cards, search, status filter and tiles or list (Spec 10.1).

    The summary counts all articles of the view; the filter counts follow the search; the list
    shows what passes both. The archive has no status filter: its badge is "archived".
    """
    if (redirect := listing.remember_layout(request, "articles")) is not None:
        return redirect  # ?layout=... only sets the cookie
    archived = view == "archived"
    states = STATES["article"]
    status = None if archived else listing.pick(request.query_params.get("status"), states)
    every = catalog.list_articles(db, archived=archived)
    found = [row for row in every if listing.matches(q, row.name, row.sku)]
    counts = Counter(row.status for row in found)
    lang = current_language(request)
    if archived:
        cards = kpis.state_cards(
            lambda key: translate(lang, key), "article", "archive", len(every),
            translate(lang, "kpi.articles_archived"), [],
        )
    else:  # "enough" and "paused" get no card: the cards name what needs attention
        cards = kpis.state_cards(
            lambda key: translate(lang, key), "article", "package", len(every),
            translate(lang, "kpi.articles"),
            summarize((row.status for row in every), ("overbooked", "below_min", "low")),
        )
    keep = (("status", status), ("view", "archived" if archived else None))
    layout = listing.layout_of(request, "articles")
    return render(
        request, "articles_list.html", active_nav="articles", archived=archived,
        rows=[row for row in found if status is None or row.status == status],
        total=len(every),
        kpis=cards,
        filters=listing.filter_links(request, states, counts, len(found), status),
        layout=layout,
        layout_links=listing.layout_links(request, layout),
        q=q,
        status=status,
        search_keep=[(name, value) for name, value in keep if value],  # a new search keeps these
    )


# ---- creation: browse folders and files, choose a plate ------------------------------------------


async def _browse(request: Request, folder: str | None, base: str, file_base: str):
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing")
    try:
        folder_id = bounded_int(folder)
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
        base=base,
        file_base=file_base,
    )


@router.get("/articles/new")
async def articles_new(request: Request, folder: str | None = None):
    return await _browse(request, folder, "/articles/new", "/articles/new/file")


def _taken_plates(db: Session, file_id: int) -> dict[int, str]:
    rows = db.execute(
        select(Article.plate_id, Article.name).where(
            Article.library_file_id == file_id, Article.archived_at.is_(None)
        )
    ).all()
    return {plate_id: name for plate_id, name in rows}


async def _plates_page(
    request: Request, db: Session, file_id: int, *, file_base: str, back_base: str,
    relink_id: int | None = None, error: str | None = None, status: int = 200,
):
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
        status_code=status,
        active_nav="articles",
        file=file,
        plates=plates,
        taken=_taken_plates(db, file_id),
        file_base=file_base,
        relink_id=relink_id,
        error=error,
        back=f"{back_base}?folder={file.folder_id}" if file.folder_id is not None else back_base,
    )


@router.get("/articles/new/file/{file_id:int}")
async def articles_plates(request: Request, file_id: int, db: Session = Depends(get_db)):
    return await _plates_page(request, db, file_id, file_base="/articles/new/file", back_base="/articles/new")


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


@router.get("/articles/{article_id:int}/relink/file/{file_id:int}/plate/{index:int}/thumbnail")
async def relink_thumbnail(request: Request, article_id: int, file_id: int, index: int):
    return await articles_thumbnail(request, file_id, index)


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


# ---- detail, thumbnail, settings -------------------------------------------------------------


def _get_article(db: Session, article_id: int) -> Article:
    article = db.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404)
    return article


def _target_label(lang: str, article: Article) -> str:
    value = catalog.target_value(article.target_model, article.printer_id)
    if value == "":
        return translate(lang, "articles.target_none")
    if value.startswith("model:"):
        return translate(lang, "articles.target_any", model=article.target_model)
    return translate(lang, "articles.target_printer_id", id=article.printer_id)


def _detail_page(request: Request, db: Session, article: Article, error: str | None = None, status: int = 200,
                 relink_offer: bool = False):
    lang = current_language(request)
    article_id = article.id
    filaments = db.scalars(
        select(ArticleFilament).where(ArticleFilament.article_id == article_id).order_by(ArticleFilament.slot)
    ).all()
    parts = db.execute(
        select(Part.name, ArticlePart.qty_per_unit, Part.unit)
        .join(ArticlePart, ArticlePart.part_id == Part.id)
        .where(ArticlePart.article_id == article_id)
        .order_by(Part.name)
    ).all()
    entries = stock_mod.journal_page(db, article_id=article_id, limit=50)
    return render(
        request,
        "article_detail.html",
        status_code=status,
        active_nav="articles",
        article=article,
        row=catalog.article_row(article),  # numbers and state, the same as in the list
        target_label=_target_label(lang, article),
        filaments=[_requirement_row(FilamentRequirement(f.slot, f.grams, f.material, f.color_hex)) for f in filaments],
        parts=parts,
        rows=journal_rows(db, entries),
        error=error,
        relink_offer=relink_offer,
    )


@router.get("/articles/{article_id:int}")
async def article_detail(request: Request, article_id: int, db: Session = Depends(get_db)):
    return _detail_page(request, db, _get_article(db, article_id))


@router.get("/articles/{article_id:int}/thumbnail")
async def article_thumbnail(article_id: int, db: Session = Depends(get_db)):
    article = db.get(Article, article_id)
    if article is None or not article.thumbnail:
        return Response(status_code=404)
    return Response(article.thumbnail, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


async def _printers_for_edit(request: Request) -> list[Printer] | None:
    """The printers for the print-target choice; None when Bambuddy cannot be asked."""
    client = get_bambuddy(request)
    return await _printers(client) if client is not None else None


def _edit_page(request, db, article, printers, values, error=None, status=200):
    lang = current_language(request)
    selected = str(values["target"])
    options = catalog.target_options(printers, None, current=selected)
    parts = active_parts(db)
    return render(
        request,
        "article_edit.html",
        status_code=status,
        active_nav="articles",
        article=article,
        target_choices=target_choices(lang, options, selected),
        printers_unavailable=printers is None,
        parts=parts,
        values=values,
        error=error,
    )


def _article_values(db: Session, article: Article, part_ids: list[int]) -> dict[str, object]:
    links = dict(db.execute(select(ArticlePart.part_id, ArticlePart.qty_per_unit).where(ArticlePart.article_id == article.id)).all())
    data = ArticleInput(
        name=article.name, sku=article.sku, units_per_print=article.units_per_print,
        min_stock=article.min_stock, buffer=article.buffer, target_model=article.target_model,
        printer_id=article.printer_id, needs_assembly=article.needs_assembly,
        auto_enabled=article.auto_enabled, parts=links,
    )
    return values_of_input(data, part_ids)


def _edit_refused(request: Request, db: Session, article: Article):
    """An archived article is not edited: restore it first."""
    return _detail_page(
        request, db, article, error=translate(current_language(request), "articles.edit_archived")
    )


@router.get("/articles/{article_id:int}/edit")
async def article_edit(request: Request, article_id: int, db: Session = Depends(get_db)):
    article = _get_article(db, article_id)
    if article.archived_at is not None:
        return _edit_refused(request, db, article)
    printers = await _printers_for_edit(request)
    part_ids = [p.id for p in active_parts(db)]
    return _edit_page(request, db, article, printers, _article_values(db, article, part_ids))


@router.post("/articles/{article_id:int}/edit", dependencies=[Depends(verify_csrf)])
async def article_update(request: Request, article_id: int, db: Session = Depends(get_db)):
    article = _get_article(db, article_id)
    if article.archived_at is not None:
        return _edit_refused(request, db, article)
    form = await request.form()
    part_ids = [p.id for p in active_parts(db)]
    stored = catalog.target_value(article.target_model, article.printer_id)
    try:
        data = parse_article_form(form, part_ids, default_target=stored)
        with write_transaction(request) as tx:
            catalog.update_article(tx, article_id, data)
    except DomainError as exc:
        printers = await _printers_for_edit(request)
        return _edit_page(
            request, db, article, printers, typed_values(form, part_ids),
            error=error_text(current_language(request), exc), status=error_status(exc),
        )
    return saved_redirect(request, f"/articles/{article_id}", "articles.saved", name=data.name)


# ---- archive, restore, restore with another file (Spec 5.5) ------------------------------------------


def _archive_action(action, flash_key: str):
    async def handler(request: Request, article_id: int, db: Session = Depends(get_db)):
        article = _get_article(db, article_id)

        def work(tx):
            action(tx, article_id)
            return {"name": article.name}

        def on_error(text: str, status: int, exc: Exception):
            # a taken plate offers the way out: restore with another file
            offer = getattr(exc, "key", None) == "articles.restore_plate_taken"
            return _detail_page(request, db, article, error=text, status=status, relink_offer=offer)

        return await run_form_action(
            request, work=work, success_url=f"/articles/{article_id}", flash_key=flash_key, on_error=on_error
        )

    return handler


router.add_api_route(
    "/articles/{article_id:int}/archive", _archive_action(catalog.archive_article, "articles.flash_archived"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)
router.add_api_route(
    "/articles/{article_id:int}/restore", _archive_action(catalog.restore_article, "articles.flash_restored"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)


def _archived_article(db: Session, article_id: int) -> Article:
    article = _get_article(db, article_id)
    if article.archived_at is None:
        raise HTTPException(status_code=404)
    return article


@router.get("/articles/{article_id:int}/relink")
async def relink_browse(request: Request, article_id: int, folder: str | None = None, db: Session = Depends(get_db)):
    _archived_article(db, article_id)
    base = f"/articles/{article_id}/relink"
    return await _browse(request, folder, base, f"{base}/file")


@router.get("/articles/{article_id:int}/relink/file/{file_id:int}")
async def relink_plates(request: Request, article_id: int, file_id: int, db: Session = Depends(get_db)):
    _archived_article(db, article_id)
    base = f"/articles/{article_id}/relink"
    return await _plates_page(
        request, db, file_id, file_base=f"{base}/file", back_base=base, relink_id=article_id
    )


@router.post(
    "/articles/{article_id:int}/relink/file/{file_id:int}/plate/{index:int}",
    dependencies=[Depends(verify_csrf)],
)
async def relink_restore(request: Request, article_id: int, file_id: int, index: int, db: Session = Depends(get_db)):
    article = _archived_article(db, article_id)
    base = f"/articles/{article_id}/relink"
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing")
    try:  # read fresh from Bambuddy, outside of the write transaction
        read = await _read_plate(client, file_id, index)
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error")
    if read is None:
        return _problem(request, "articles.file_missing")
    file, plate, requirements, thumbnail = read

    def work(tx):
        catalog.restore_article_with_plate(
            tx, article_id, file=file, plate=plate, requirements=requirements, thumbnail=thumbnail
        )
        return {"name": article.name}

    return await run_form_action(
        request,
        work=work,
        success_url=f"/articles/{article_id}",
        flash_key="articles.flash_restored",
        on_error=lambda text, status, exc: _plates_page(
            request, db, file_id, file_base=f"{base}/file", back_base=base, relink_id=article_id,
            error=text, status=status,
        ),
    )
