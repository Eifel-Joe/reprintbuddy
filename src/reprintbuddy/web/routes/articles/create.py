"""Creating an article: browse the print files, choose a plate, fill the form (Spec 5.1, 5.2)."""

from __future__ import annotations

import re
from collections.abc import Mapping

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate, Printer
from reprintbuddy.domain import catalog, articlesets
from reprintbuddy.domain.printfile import is_external_folder
from reprintbuddy.models import Article, Part
from reprintbuddy.web import listing
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_bambuddy, get_db, render
from reprintbuddy.web.forms import bounded_int, run_form_action, saved_redirect
from reprintbuddy.web.flash import flash
from reprintbuddy.web.filament_view import BAMBUDDY_ERRORS, COLOR
from reprintbuddy.web.routes.upload import upload_url
from reprintbuddy.web.routes.articles.form import (
    NEW_ARTICLE,
    NotSliced,
    _printers,
    _problem,
    _read_plate,
    active_parts,
    change_part_rows,
    form_error,
    parse_article_form,
    target_choices,
    typed_values,
    values_of_input,
)

router = APIRouter()


# ---- creation: browse folders and files, choose a plate ------------------------------------------


async def _browse(request: Request, folder: str | None, base: str, file_base: str, *, relink: bool = False,
                  article_id: int | None = None):
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
    # Uploading into the open folder (Spec 5.1a), also when choosing another file for an article (it then
    # goes on to that article's plates); not in a folder linked from outside Bambuddy (its files have no
    # fingerprint after a rescan).
    upload_href = None
    if not is_external_folder(folders, folder_id) and (folder_id is None or crumbs):
        upload_href = upload_url(folder_id, article_id)
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
        upload_href=upload_href,
        external=is_external_folder(folders, folder_id),  # p169: the files here are chosen with a warning
    )


@router.get("/articles/new")
async def articles_new(request: Request, folder: str | None = None):
    return await _browse(request, folder, "/articles/new", "/articles/new/file")


def _taken_plates(db: Session, file_id: int, besides: int | None = None) -> dict[int, str]:
    """Plates of the file held by an active article; `besides` (the one relinked) is left
    out, so it can choose its own plate again, as the domain allows (`_plate_holder`)."""
    query = select(Article.plate_id, Article.name).where(
        Article.library_file_id == file_id, articlesets.where_print()
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
    # p169: a file from a folder linked from outside (or below one) is chosen with a warning; the folder
    # list is read fresh, as the block on uploading does. It serves the warning alone: when it cannot be
    # read the page shows without the warning instead of failing.
    try:
        external = is_external_folder(await client.list_folders(), file.folder_id)
    except BAMBUDDY_ERRORS:
        external = False
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
        external=external,
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
        "grams": r.grams,  # shown through the `num` filter (decimal separator by language)
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

    def save(tx, data):
        article = catalog.create_article(
            tx, file=file, plate=plate, requirements=requirements, thumbnail=thumbnail, data=data
        )
        return article.id, article.name

    def created(result):
        article_id, article_name = result
        # p153: a new article has no spool sorts ticked yet, and it is easy to forget (the article would
        # show "not assigned" for good): the filament page comes first, its save leads to the article.
        # A plate without filament slots has nothing to assign. A new article is never archived.
        if not requirements:
            return saved_redirect(request, f"/articles/{article_id}", "articles.created", name=article_name)
        flash(request, "articles.created", name=article_name)
        flash(request, "articles.created_filament")
        return RedirectResponse(f"/articles/{article_id}/filaments{NEW_ARTICLE}", status_code=303)

    async def refused(text, status, exc):
        printers = await _printers(client)
        error, field_errors = form_error(lang, exc)
        return _plate_form(
            request, action=action, file=file, plate=plate, requirements=requirements,
            printers=printers, parts=parts, values=values,
            error=error, field_errors=field_errors, status=status,
        )

    return await run_form_action(
        request,
        check=lambda: parse_article_form(form, parts, default_target=catalog.target_value(file.printer_model, None)),
        work=save, done=created, duplicate_url="/articles", on_error=refused,
    )
