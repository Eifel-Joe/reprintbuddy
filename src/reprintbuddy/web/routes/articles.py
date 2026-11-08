"""Articles: list and creation from a Bambuddy print file (Spec 5.1, 5.2).

Bambuddy is only read here, through `BambuddyClient`. When creating, the file, the
plates, the filament need and the preview are read again from Bambuddy on submit
and nothing is taken from hidden fields.
"""

from __future__ import annotations

import re
from collections import Counter
from itertools import zip_longest
from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected
from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate, Printer
from reprintbuddy.domain import catalog, filament, pauses, workqueue
from reprintbuddy.domain import color as color_mod
from reprintbuddy.domain.catalog import MAX_LABEL_TEXT, ArticleInput, TargetOption
from reprintbuddy.domain.errors import DomainError, NotFoundError, ValidationError
from reprintbuddy.domain.status import STATES, summarize
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.domain import stock as stock_mod
from reprintbuddy.models import (
    Article, ArticleFilament, ArticleFilamentSort, ArticlePart, Dispatch, FilamentSort, Part, WorkQueueEntry,
)
from reprintbuddy.web import kpis, labels_view, listing
from reprintbuddy.web import qr as qr_view
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_bambuddy, get_db, local_next, release_connection, render
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
from reprintbuddy.web.flash import flash
from reprintbuddy.web.formatting import format_date
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction
from reprintbuddy.web.filament_view import BAMBUDDY_ERRORS, COLOR, spools_or_none
from reprintbuddy.web.journal_view import journal_rows
from reprintbuddy.web.routes.queue import DISPATCH_TONES
from reprintbuddy.web.i18n import translate

router = APIRouter()


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
    t = lambda key: translate(lang, key)  # noqa: E731
    # "enough" and "paused" get no card: the cards name what needs attention
    icon, label, named = ("archive", "kpi.articles_archived", []) if archived else (
        "package", "kpi.articles", summarize((row.status for row in every), ("overbooked", "below_min", "low"))
    )
    cards = kpis.state_cards(t, "article", icon, len(every), t(label), named)
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
        here=request.url.path + (f"?{request.url.query}" if request.url.query else ""),  # the switches return here
    )


# ---- creation: browse folders and files, choose a plate ------------------------------------------


async def _browse(request: Request, folder: str | None, base: str, file_base: str, *, relink: bool = False):
    if (redirect := listing.remember_layout(request, "files")) is not None:
        return redirect  # ?layout=... only sets the cookie
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing", relink=relink)
    try:
        folder_id = bounded_int(folder)
        folders = await client.list_folders()
        files = await client.list_files(folder_id)
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error", relink=relink)
    by_id = {f.id: f for f in folders}
    crumbs, seen, current = [], set(), by_id.get(folder_id) if folder_id is not None else None
    while current is not None and current.id not in seen:
        seen.add(current.id)
        crumbs.append(current)
        current = by_id.get(current.parent_id) if current.parent_id is not None else None
    crumbs.reverse()
    subfolders = sorted((f for f in folders if f.parent_id == folder_id), key=lambda f: f.name.casefold())
    layout = listing.layout_of(request, "files")
    return render(
        request,
        "article_browse.html",
        active_nav="articles",
        problem=None,
        relink=relink,
        crumbs=crumbs,
        up_id=crumbs[-2].id if len(crumbs) > 1 else None,
        in_folder=bool(crumbs),
        subfolders=subfolders,
        # Only sliced files can become an article, so only they are offered (Task 12); folders stay for navigation.
        files=sorted((f for f in files if f.sliced), key=lambda f: f.filename.casefold()),
        hidden_unsliced=sum(1 for f in files if not f.sliced),  # p42: the hint says they exist
        base=base,
        file_base=file_base,
        layout=layout,
        layout_links=listing.layout_links(request, layout),
    )


@router.get("/articles/new")
async def articles_new(request: Request, folder: str | None = None):
    return await _browse(request, folder, "/articles/new", "/articles/new/file")


def _taken_plates(db: Session, file_id: int, besides: int | None = None) -> dict[int, str]:
    """Plates of the file held by an active article; `besides` (the one relinked) is left
    out, so it can choose its own plate again, as the domain allows (`_plate_holder`)."""
    query = select(Article.plate_id, Article.name).where(
        Article.library_file_id == file_id, Article.archived_at.is_(None)
    )
    if besides is not None:
        query = query.where(Article.id != besides)
    rows = db.execute(query).all()
    return {plate_id: name for plate_id, name in rows}


async def _plates_page(
    request: Request, db: Session, file_id: int, *, file_base: str, back_base: str,
    relink_id: int | None = None, relink_archived: bool = True, error: str | None = None, status: int = 200,
):
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing", relink=relink_id is not None)
    try:
        file = await client.get_file(file_id)
        plates = await client.list_plates(file_id) if file is not None and file.sliced else []
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error", relink=relink_id is not None)
    if file is None:
        return _problem(request, "articles.file_missing", 404, relink=relink_id is not None)
    if not file.sliced:
        return _problem(request, "articles.not_sliced", 409, relink=relink_id is not None)
    return render(
        request,
        "article_plates.html",
        status_code=status,
        active_nav="articles",
        file=file,
        plates=plates,
        taken=_taken_plates(db, file_id, besides=relink_id),
        file_base=file_base,
        relink_id=relink_id,
        relink_archived=relink_archived,
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


@router.get("/articles/new/file/{file_id:int}/thumbnail")
async def articles_file_thumbnail(request: Request, file_id: int):
    """The preview of a print file in the file choice (also used when relinking). Any problem is a
    404, so the page keeps its placeholder; nothing is stored here."""
    client = get_bambuddy(request)
    if client is None:
        return Response(status_code=404)
    try:
        content_type, data = await client.file_thumbnail(file_id)
    except BAMBUDDY_ERRORS:
        return Response(status_code=404)
    return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=300"})


_COLOR = re.compile(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?")


def _requirement_row(r: FilamentRequirement) -> dict[str, object]:
    """One row of the filament table; the colour is only shown as a swatch when it is a plain hex value."""
    return {
        "slot": r.slot,
        "material": r.material or "",
        "grams": f"{r.grams:.1f}",
        "color": r.color_hex or "",
        "swatch": r.color_hex if r.color_hex and COLOR.fullmatch(r.color_hex) else None,
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
    field_errors: Mapping[str, str] | None = None,
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
        field_errors=field_errors or {},
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
    except NotSliced:
        return _problem(request, "articles.not_sliced", 409)
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error")
    if read is None:
        return _problem(request, "articles.file_missing", 404)
    file, plate, requirements, _thumbnail = read
    parts = active_parts(db)
    values = values_of_input(catalog.default_article_input(file), parts)
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
    except NotSliced:
        return _problem(request, "articles.not_sliced", 409)
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error")
    if read is None:
        return _problem(request, "articles.file_missing")
    file, plate, requirements, thumbnail = read
    parts = active_parts(db)
    action = f"/articles/new/file/{file_id}/plate/{index}"
    lang = current_language(request)
    values = typed_values(form)
    if change_part_rows(form, values):
        return _plate_form(
            request, action=action, file=file, plate=plate, requirements=requirements,
            printers=await _printers(client), parts=parts, values=values,
        )
    try:
        data = parse_article_form(
            form, parts, default_target=catalog.target_value(file.printer_model, None)
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
        error, field_errors = form_error(lang, exc)
        return _plate_form(
            request, action=action, file=file, plate=plate, requirements=requirements,
            printers=printers, parts=parts, values=values,
            error=error, field_errors=field_errors, status=error_status(exc),
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
    ticked: dict[int, list[ArticleFilamentSort]] = {}
    for row in db.scalars(select(ArticleFilamentSort).where(ArticleFilamentSort.article_id == article_id)):
        ticked.setdefault(row.slot, []).append(row)
    sort_labels = dict(db.execute(select(FilamentSort.id, FilamentSort.label)).all())

    def uses(slot: int) -> str | None:
        """The reordered spool sort of a slot, with how many more are ticked; None: nothing ticked."""
        rows = ticked.get(slot, [])
        reordered = next((r.sort_id for r in rows if r.reordered), None)
        if reordered is None:
            return None
        label = sort_labels.get(reordered, str(reordered))
        more = len(rows) - 1
        return translate(lang, "filament.and_more", label=label, n=more) if more else label
    parts = db.execute(
        select(Part.name, ArticlePart.qty_per_unit, Part.unit)
        .join(ArticlePart, ArticlePart.part_id == Part.id)
        .where(ArticlePart.article_id == article_id)
        .order_by(Part.name)
    ).all()
    entries = stock_mod.journal_page(db, article_id=article_id, limit=50)
    coverage = workqueue.coverage_of(db, article)
    queue_entries = db.scalars(
        select(WorkQueueEntry)
        .where(WorkQueueEntry.article_id == article_id)
        .order_by(WorkQueueEntry.position, WorkQueueEntry.id)
    ).all()
    open_prints = db.scalars(
        select(Dispatch)
        .where(Dispatch.article_id == article_id, Dispatch.status.in_(workqueue.OPEN_STATES))
        .order_by(Dispatch.id)
    ).all()
    # numbers and state, the same as in the list (full coverage, Spec 7.1 and 10.1)
    row = catalog.article_row(
        article,
        queued_units=coverage.queued_units,
        in_progress_units=coverage.in_progress_units,
        in_progress_prints=len(open_prints),
    )
    base = qr_view.base_address(request, db)
    qr_address = qr_view.article_address(base, article_id)
    return render(
        request,
        "article_detail.html",
        status_code=status,
        active_nav="articles",
        article=article,
        row=row,
        queue_entries=queue_entries,
        open_prints=open_prints,
        dispatch_tones=DISPATCH_TONES,
        qr_address=qr_address,
        qr_local=qr_view.is_local_address(base),
        **labels_view.article_card_context(request, db, article, lang),
        has_target=bool(article.target_model or article.printer_id),
        target_label=_target_label(lang, article),
        filaments=[
            _requirement_row(FilamentRequirement(f.slot, f.grams, f.material, f.color_hex))
            | {"sort": uses(f.slot)}
            for f in filaments
        ],
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


def _edit_page(request, db, article, printers, values, error=None, status=200, field_errors=None):
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
        field_errors=field_errors or {},
        target_choices=target_choices(lang, options, selected),
        printers_unavailable=printers is None,
        parts=parts,
        values=values,
        error=error,
    )


def _article_values(db: Session, article: Article, parts: list[Part]) -> dict[str, object]:
    links = dict(db.execute(select(ArticlePart.part_id, ArticlePart.qty_per_unit).where(ArticlePart.article_id == article.id)).all())
    data = ArticleInput(
        name=article.name, sku=article.sku, label_text=article.label_text,
        units_per_print=article.units_per_print,
        min_stock=article.min_stock, buffer=article.buffer, target_model=article.target_model,
        printer_id=article.printer_id, needs_assembly=article.needs_assembly,
        auto_enabled=article.auto_enabled, parts=links,
    )
    return values_of_input(data, parts)


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
    return _edit_page(request, db, article, printers, _article_values(db, article, active_parts(db)))


@router.post("/articles/{article_id:int}/edit", dependencies=[Depends(verify_csrf)])
async def article_update(request: Request, article_id: int, db: Session = Depends(get_db)):
    article = _get_article(db, article_id)
    if article.archived_at is not None:
        return _edit_refused(request, db, article)
    form = await request.form()
    parts = active_parts(db)
    values = typed_values(form)
    if change_part_rows(form, values):
        return _edit_page(request, db, article, await _printers_for_edit(request), values)
    stored = catalog.target_value(article.target_model, article.printer_id)
    try:
        data = parse_article_form(form, parts, default_target=stored)
        with write_transaction(request) as tx:
            catalog.update_article(tx, article_id, data)
    except DomainError as exc:
        printers = await _printers_for_edit(request)
        error, field_errors = form_error(current_language(request), exc)
        return _edit_page(
            request, db, article, printers, values,
            error=error, field_errors=field_errors, status=error_status(exc),
        )
    return saved_redirect(request, f"/articles/{article_id}", "articles.saved", name=data.name)


# ---- the filament group of each slot (Spec 5.2, 9.1) -------------------------------------------

_Typed = Mapping[int, tuple[set[str], str]]


def _choice(sort: FilamentSort, stock: filament.SortStock | None, known: bool) -> dict[str, object]:
    """One spool sort to tick: brand, subtype, colour and stock (Spec 9.1)."""
    name = (sort.color_name or "").strip()
    value = color_mod.display_hex(sort.color_hex)
    return {
        "id": str(sort.id),
        "brand": sort.brand or "",
        "subtype": sort.subtype or "",
        "color": name or value,
        "value": value if name else "",  # shown when two choices of a slot read alike
        "swatch": sort.color_hex if sort.color_hex and COLOR.fullmatch(sort.color_hex) else None,
        "stock": f"{stock.stock_g:.0f}" if known and stock is not None else None,
    }


def _tell_apart(choices: list[dict[str, object]]) -> None:
    """Brand, subtype and colour name alike: the row and its radio name the colour value too."""
    seen = Counter((c["brand"], c["subtype"], c["color"]) for c in choices)
    for c in choices:
        c["show_value"] = bool(c["value"]) and seen[(c["brand"], c["subtype"], c["color"])] > 1
        words = [c["brand"], c["subtype"], c["color"], c["value"] if c["show_value"] else ""]
        c["name"] = " ".join(str(w) for w in words if w)


def _filament_page(request: Request, spools, article_id: int, typed: _Typed | None = None,
                   error: str | None = None, status: int = 200):
    """The form to tick the spool sorts of each slot. Reads (and enters new spool sorts, Entscheidung 4)
    in one write transaction; `typed` holds what a rejected submission ticked, which stays as it was."""
    lang = current_language(request)
    with write_transaction(request) as tx:
        article = tx.get(Article, article_id)
        if article is None:
            raise HTTPException(status_code=404)
        sorts, day = filament.known_sorts(tx, spools)
        groups = filament.slot_groups(tx, article_id)
        by_id = {s.id: s for s in tx.scalars(select(FilamentSort))}
        name, archived = article.name, article.archived_at is not None
        slots = []
        for g in groups:
            if typed is not None and g.slot in typed:
                ticked, reordered = typed[g.slot]
            else:
                ticked = {str(i) for i in g.ticked}
                reordered = str(g.reordered) if g.reordered is not None else ""
            choices = [
                _choice(by_id[i], (sorts or {}).get(i), sorts is not None)
                | {"ticked": str(i) in ticked, "reordered": str(i) == reordered}
                for i in g.choices
            ]
            _tell_apart(choices)
            slots.append({
                "slot": g.slot,
                "grams": f"{g.grams:.1f}",
                "material": g.material or "",
                "color": color_mod.display_hex(g.color_hex) if g.color_hex else "",
                "swatch": g.color_hex if g.color_hex and COLOR.fullmatch(g.color_hex) else None,
                "choices": choices,
            })
    if archived and error is None:
        error = translate(lang, "articles.edit_archived")
    return render(
        request, "article_filaments.html", status_code=status, active_nav="articles",
        article_id=article_id, article_name=name, slots=slots, sorts_unknown=sorts is None,
        stale_day=format_date(day, lang) if day else None, read_only=archived, error=error,
    )


@router.get("/articles/{article_id:int}/filaments")
async def article_filaments(request: Request, article_id: int, db: Session = Depends(get_db)):
    _get_article(db, article_id)
    spools = await spools_or_none(request)  # before any write transaction
    return _filament_page(request, spools, article_id)


@router.post("/articles/{article_id:int}/filaments", dependencies=[Depends(verify_csrf)])
async def article_filaments_save(request: Request, article_id: int, db: Session = Depends(get_db)):
    """One `slot` field per slot of the form, `tick_<slot>` per ticked spool sort, `reorder_<slot>`
    for the reordered one. A slot named without ticks is cleared; slots not named stay."""
    _get_article(db, article_id)
    form = await request.form()
    typed: dict[int, tuple[set[str], str]] = {}
    lang = current_language(request)
    try:
        groups: dict[int, tuple[set[int], int | None]] = {}
        for raw_slot in form.getlist("slot"):
            slot = bounded_int(str(raw_slot))
            if slot is None:
                raise ValidationError("filament.unknown_slot")
            ticks = [str(v) for v in form.getlist(f"tick_{slot}")]
            reorder_raw = str(form.get(f"reorder_{slot}", ""))
            typed[slot] = (set(ticks), reorder_raw)
            ticked = set()
            for raw in ticks:
                if (sort_id := bounded_int(raw)) is None:
                    raise ValidationError("filament.invalid_sort")
                ticked.add(sort_id)
            reordered = bounded_int(reorder_raw) if reorder_raw else None
            if reorder_raw and reordered is None:
                raise ValidationError("filament.reordered_not_ticked")
            groups[slot] = (ticked, reordered)
        with write_transaction(request) as tx:
            filament.set_groups(tx, article_id, groups)
    except DomainError as exc:
        spools = await spools_or_none(request)  # only to show the form again, outside the write transaction
        known = {k: v for k, v in typed.items()}
        return _filament_page(request, spools, article_id, known, error_text(lang, exc), error_status(exc))
    return saved_redirect(request, f"/articles/{article_id}", "filament.mapping_saved")


# ---- archive, restore, restore with another file (Spec 5.5) ------------------------------------------


def _archive_action(action, flash_key: str):
    async def handler(request: Request, article_id: int, db: Session = Depends(get_db)):
        article = _get_article(db, article_id)
        # From the list (its forms send `next`) the person lands on the list again, where the row
        # left or came back; from the detail page (no `next`) on the detail page.
        after = local_next(await request.form()) or f"/articles/{article_id}"

        def work(tx):
            action(tx, article_id)
            return {"name": article.name}

        def on_error(text: str, status: int, exc: Exception):
            # a taken plate offers the way out: restore with another file
            offer = getattr(exc, "key", None) == "articles.restore_plate_taken"
            return _detail_page(request, db, article, error=text, status=status, relink_offer=offer)

        return await run_form_action(request, work=work, success_url=after, flash_key=flash_key, on_error=on_error)

    return handler


router.add_api_route(
    "/articles/{article_id:int}/archive", _archive_action(catalog.archive_article, "articles.flash_archived"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)
router.add_api_route(
    "/articles/{article_id:int}/restore", _archive_action(catalog.restore_article, "articles.flash_restored"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)


# Choosing the file again: an archived article is restored with it (Spec 5.5), an active one
# keeps its numbers and only changes file and plate (Spec 5.1, plan B4 decision 10).


@router.get("/articles/{article_id:int}/relink")
async def relink_browse(request: Request, article_id: int, folder: str | None = None, db: Session = Depends(get_db)):
    _get_article(db, article_id)
    release_connection(db)  # nothing is pending; no connection is held while Bambuddy answers
    base = f"/articles/{article_id}/relink"
    return await _browse(request, folder, base, f"{base}/file", relink=True)


@router.get("/articles/{article_id:int}/relink/file/{file_id:int}")
async def relink_plates(request: Request, article_id: int, file_id: int, db: Session = Depends(get_db)):
    archived = _get_article(db, article_id).archived_at is not None
    release_connection(db)  # nothing is pending; no connection is held while Bambuddy answers
    base = f"/articles/{article_id}/relink"
    return await _plates_page(
        request, db, file_id, file_base=f"{base}/file", back_base=base, relink_id=article_id,
        relink_archived=archived,
    )


@router.post(
    "/articles/{article_id:int}/relink/file/{file_id:int}/plate/{index:int}",
    dependencies=[Depends(verify_csrf)],
)
async def relink_restore(request: Request, article_id: int, file_id: int, index: int, db: Session = Depends(get_db)):
    article = _get_article(db, article_id)
    archived, name = article.archived_at is not None, article.name
    base = f"/articles/{article_id}/relink"
    release_connection(db)  # nothing is pending; no connection is held while Bambuddy answers
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing", relink=True)
    try:  # read fresh from Bambuddy, outside of the write transaction
        read = await _read_plate(client, file_id, index)
    except NotSliced:
        return _problem(request, "articles.not_sliced", 409, relink=True)
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error", relink=True)
    if read is None:
        return _problem(request, "articles.file_missing", relink=True)
    file, plate, requirements, thumbnail = read

    def work(tx):
        if archived:
            catalog.restore_article_with_plate(
                tx, article_id, file=file, plate=plate, requirements=requirements, thumbnail=thumbnail
            )
        else:
            catalog.relink_article(
                tx, article_id, file=file, plate=plate, requirements=requirements, thumbnail=thumbnail,
                actor="user",
            )
        return {"name": name}

    return await run_form_action(
        request,
        work=work,
        success_url=f"/articles/{article_id}",
        flash_key="articles.flash_restored" if archived else "articles.relinked",
        on_error=lambda text, status, exc: _plates_page(
            request, db, file_id, file_base=f"{base}/file", back_base=base, relink_id=article_id,
            relink_archived=archived, error=text, status=status,
        ),
    )


# ---- pause of the automation and the print file (Spec 5.1, plan B4 decision 8) ----------------


def _detail_refused(request: Request, db: Session, article_id: int, text: str, status: int = 200):
    return _detail_page(request, db, _get_article(db, article_id), error=text, status=status)


@router.post("/articles/{article_id:int}/resume", dependencies=[Depends(verify_csrf)])
async def article_resume(request: Request, article_id: int, db: Session = Depends(get_db)):
    """Lift the pause. A file pause or a missing print target refuses (`pauses.lift_pause`).

    Back to the page the button was on (`next`, a local path; the notice of a pause on the
    overview and the queue has one), else to the article. A refusal shows on the article page."""
    name = _get_article(db, article_id).name
    form = await request.form()
    # The overview is "/", which `local_next` counts as no `next`: here it is a page like any.
    back = "/" if str(form.get("next", "")) == "/" else local_next(form)
    after = back or f"/articles/{article_id}"

    def work(tx):
        pauses.lift_pause(tx, article_id, "user")
        return {"name": name}

    return await run_form_action(
        request, work=work, success_url=after, flash_key="articles.resumed",
        on_error=lambda text, status, exc: _detail_refused(request, db, article_id, text, status),
    )


@router.post("/articles/{article_id:int}/auto", dependencies=[Depends(verify_csrf)])
async def article_auto(request: Request, article_id: int, db: Session = Depends(get_db)):
    """The automation switch of the list (p44): `enabled=1|0` is the wanted state, never a toggle.

    Back to the list the switch was on (`next`, with its search and filter); a refusal is a
    warning there, not a page of its own."""
    name = _get_article(db, article_id).name
    form = await request.form()
    after = local_next(form) or "/articles"
    wanted = str(form.get("enabled", "")).strip()

    def work(tx):
        if wanted not in ("0", "1"):
            raise ValidationError("articles.auto_bad_value")
        catalog.set_auto_enabled(tx, article_id, wanted == "1")
        return {"name": name}

    def refused(text: str, status: int, exc: Exception):
        if isinstance(exc, DomainError):
            flash(request, exc.key, warning=True, **{k: v for k, v in exc.params.items() if isinstance(v, (str, int))})
        else:  # an expired or forged form token
            flash(request, "form.expired", warning=True)
        return RedirectResponse(after, status_code=303)

    return await run_form_action(
        request, work=work, success_url=after,
        flash_key="articles.auto_on" if wanted == "1" else "articles.auto_off", on_error=refused,
    )


@router.post("/articles/{article_id:int}/confirm-file", dependencies=[Depends(verify_csrf)])
async def article_confirm_file(request: Request, article_id: int, db: Session = Depends(get_db)):
    """Accept the changed print file as it is now (Spec 5.1): file, filament need and preview
    are read from Bambuddy first, outside of the write transaction."""
    article = _get_article(db, article_id)
    name, file_id, plate_index = article.name, article.library_file_id, article.plate_id
    release_connection(db)  # nothing is pending; no connection is held while Bambuddy answers

    def refused(key: str):
        return _detail_refused(request, db, article_id, translate(current_language(request), key))

    client = get_bambuddy(request)
    if client is None:
        return refused("articles.bambuddy_missing")
    try:
        read = await _read_plate(client, file_id, plate_index)
    except BAMBUDDY_ERRORS:
        return refused("articles.bambuddy_error")
    if read is None:  # the file or its plate is gone: only choosing again helps
        return refused("articles.file_missing")
    file, _plate, requirements, thumbnail = read

    def work(tx):
        catalog.confirm_article_file(
            tx, article_id, file=file, requirements=requirements, thumbnail=thumbnail, actor="user"
        )
        return {"name": name}

    return await run_form_action(
        request, work=work, success_url=f"/articles/{article_id}", flash_key="articles.file_confirmed",
        on_error=lambda text, status, exc: _detail_refused(request, db, article_id, text, status),
    )
