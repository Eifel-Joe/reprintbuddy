"""Article detail, thumbnail, settings and the filament group of each slot (Spec 5.2, 9.1)."""

from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.bambuddy.types import FilamentRequirement, Printer
from reprintbuddy.domain import (
    assembled,
    catalog,
    filament,
    workqueue,
    color as color_mod,
    stock as stock_mod,
)
from reprintbuddy.domain.catalog import ArticleInput
from reprintbuddy.domain.errors import DomainError, ValidationError
from reprintbuddy.models import Article, ArticlePart, Dispatch, FilamentSort, Part, WorkQueueEntry
from reprintbuddy.web import labels_view, qr as qr_view
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import (
    get_or_404,
    current_language,
    db_session,
    get_bambuddy,
    get_db,
    render,
)
from reprintbuddy.web.forms import bounded_int, error_status, error_text, saved_redirect
from reprintbuddy.web.formatting import format_date
from reprintbuddy.web.formtoken import write_transaction
from reprintbuddy.web.filament_view import COLOR, may_write, page_spools
from reprintbuddy.web.journal_view import journal_rows
from reprintbuddy.web.routes.queue import DISPATCH_TONES
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.routes.articles.form import (
    LATER,
    NEW_ARTICLE,
    _printers,
    active_parts,
    change_part_rows,
    form_error,
    parse_article_form,
    target_choices,
    typed_values,
    values_of_input,
)
from reprintbuddy.web.routes.articles.create import _requirement_row

router = APIRouter()


# ---- detail, thumbnail, settings -------------------------------------------------------------


def _relink_article(db: Session, article_id: int) -> Article:
    """A print article, or an article made of parts that becomes one again (Spec 5.6); never a variant."""
    article = get_or_404(db, Article, article_id)
    if article.kind == "variant":
        raise HTTPException(status_code=404)
    return article


def _target_label(lang: str, article: Article) -> str:
    value = catalog.target_value(article.target_model, article.printer_id)
    if value == "":
        return translate(lang, "articles.target_none")
    if value.startswith("model:"):
        return translate(lang, "articles.target_any", model=article.target_model)
    return translate(lang, "articles.target_printer_id", id=article.printer_id)


def _assembled_page(request: Request, db: Session, article: Article, error: str | None = None, status: int = 200):
    """The page of an article made of parts (Spec 5.6, 10.3; 0.4.0 p341)."""
    row = assembled.assembled_row(db, article.id)
    entries = stock_mod.journal_page(db, article_ids=assembled.journal_ids(db, article), limit=50)
    picture = db.get(Article, row.thumbnail_article_id) if row.thumbnail_article_id else None
    base = qr_view.base_address(request, db)
    return render(
        request,
        "assembled_detail.html",
        status_code=status,
        active_nav="articles",
        article=article,
        row=row,
        picture=bool(picture is not None and picture.thumbnail),
        qr_address=qr_view.article_address(base, article.id),
        **labels_view.article_card_context(db),
        rows=journal_rows(db, entries),
        error=error,
    )


def _detail_page(request: Request, db: Session, article: Article, error: str | None = None, status: int = 200,
                 relink_offer: bool = False):
    if article.kind == "assembled":
        return _assembled_page(request, db, article, error, status)
    lang = current_language(request)
    article_id = article.id
    filaments = filament.article_slots(db, article_id)
    ticked = filament.ticked_rows(db, article_id)
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
    # numbers and state, the same as in the list (full coverage, Spec 7.1 and 10.1; demand from assembly)
    row = catalog.article_row(
        article,
        queued_units=coverage.queued_units,
        in_progress_units=coverage.in_progress_units,
        in_progress_prints=len(open_prints),
        demand=assembled.assembly_demand(db).get(article.id, 0),
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
        **labels_view.article_card_context(db),
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
        used_in=assembled.used_in(db, article_id),  # Spec 10.3: the articles made of parts it is in
        demand=assembled.assembly_demand(db).get(article_id, 0),
    )


@router.get("/articles/{article_id:int}")
async def article_detail(request: Request, article_id: int, db: Session = Depends(get_db)):
    article = get_or_404(db, Article, article_id)
    if article.kind == "variant":  # a variant shows on the page of its article made of parts
        return RedirectResponse(f"/articles/{article.parent_id}", status_code=303)
    return _detail_page(request, db, article)


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
        auto_enabled=article.auto_enabled, parts=links, sold_alone=article.sold_alone,
    )
    return values_of_input(data, parts)


def _edit_refused(request: Request, db: Session, article: Article):
    """An archived article is not edited: restore it first."""
    return _detail_page(
        request, db, article, error=translate(current_language(request), "articles.edit_archived")
    )


@router.get("/articles/{article_id:int}/edit")
async def article_edit(request: Request, article_id: int, db: Session = Depends(get_db)):
    article = get_or_404(db, Article, article_id)
    if article.kind == "assembled":  # an article made of parts has its own form (Spec 5.6, 0.4.0)
        return RedirectResponse(f"/articles/{article.id}/assembled", status_code=303)
    if article.archived_at is not None:
        return _edit_refused(request, db, article)
    printers = await _printers_for_edit(request)
    return _edit_page(request, db, article, printers, _article_values(db, article, active_parts(db)))


@router.post("/articles/{article_id:int}/edit", dependencies=[Depends(verify_csrf)])
async def article_update(request: Request, article_id: int, db: Session = Depends(get_db)):
    article = get_or_404(db, Article, article_id)
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
    in one write transaction; `typed` holds what a rejected submission ticked, which stays as it was.
    While writes are paused (a restore or an update, p230) it reads the record only and writes nothing."""
    lang = current_language(request)
    later = request.query_params.get("later") == "1"  # p153: led here by a creation or a change of the file
    suggest = later and request.query_params.get("suggest") == "1"  # p156: only right after creating
    writing = may_write(request)
    if not writing:
        spools = None  # live spools would enter new sorts
    with write_transaction(request) if writing else nullcontext(db_session(request)) as tx:
        article = tx.get(Article, article_id)
        if article is None:
            raise HTTPException(status_code=404)
        sorts, day = filament.known_sorts(tx, spools)
        groups = filament.slot_groups(tx, article_id)
        suggestions = filament.suggest_from_similar(tx, article_id) if suggest and article.archived_at is None else {}
        by_id = {s.id: s for s in tx.scalars(select(FilamentSort))}
        name, archived = article.name, article.archived_at is not None
        slots = []
        for g in groups:
            suggestion = suggestions.get(g.slot)
            if typed is not None and g.slot in typed:
                ticked, reordered = typed[g.slot]
                suggestion = None  # what a rejected submission ticked stays as it was
            elif suggestion is not None and not g.ticked:  # a slot that has a group keeps it
                ticked = {str(i) for i in suggestion.sort_ids}
                reordered = str(suggestion.reordered) if suggestion.reordered is not None else ""
            else:
                suggestion = None
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
                "grams": g.grams,
                "material": g.material or "",
                "color": color_mod.display_hex(g.color_hex) if g.color_hex else "",
                "swatch": g.color_hex if g.color_hex and COLOR.fullmatch(g.color_hex) else None,
                "choices": choices,
                "suggested_from": suggestion.source_name if suggestion is not None else None,
            })
    if archived and error is None:
        error = translate(lang, "articles.edit_archived")
    return render(
        request, "article_filaments.html", status_code=status, active_nav="articles",
        article_id=article_id, article_name=name, slots=slots, sorts_unknown=sorts is None,
        stale_day=format_date(day, lang) if day else None, read_only=archived, error=error,
        # p182: recorded stock says so at the stock column, as on the filament page (p63)
        as_of=translate(lang, "filament.as_of", day=format_date(day, lang)) if day else None,
        later=later, query=(NEW_ARTICLE if suggest else LATER if later else ""),
    )


@router.get("/articles/{article_id:int}/filaments")
async def article_filaments(request: Request, article_id: int, db: Session = Depends(get_db)):
    get_or_404(db, Article, article_id)
    spools = await page_spools(request)  # before any write transaction
    return _filament_page(request, spools, article_id)


@router.post("/articles/{article_id:int}/filaments", dependencies=[Depends(verify_csrf)])
async def article_filaments_save(request: Request, article_id: int, db: Session = Depends(get_db)):
    """One `slot` field per slot of the form, `tick_<slot>` per ticked spool sort, `reorder_<slot>`
    for the reordered one. A slot named without ticks is cleared; slots not named stay."""
    get_or_404(db, Article, article_id)
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
        spools = await page_spools(request)  # only to show the form again, outside the write transaction
        known = {k: v for k, v in typed.items()}
        return _filament_page(request, spools, article_id, known, error_text(lang, exc), error_status(exc))
    return saved_redirect(request, f"/articles/{article_id}", "filament.mapping_saved")
