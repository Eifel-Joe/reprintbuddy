"""Routes for inspection and assembly (Spec 8.2, 8.3): the page "To check" and the assembly page."""

from __future__ import annotations

import json
import re
from datetime import UTC

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import inspection
from reprintbuddy.domain.errors import ConflictError, DomainError, NotFoundError, ValidationError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Article, Inspection
from reprintbuddy.web import listing
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.flash import flash
from reprintbuddy.web.forms import duplicate_redirect, error_status, error_text, parse_int
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.kpis import Kpi
from reprintbuddy.web.routes.bookings import WARNING_KEYS, _active_article, _shortages

router = APIRouter()

# The most good units of one print the JSON interface accepts as `good_units`; the domain holds the real limit.
MAX_GOOD = 1000
# An id in a form: ASCII digits only, at most 18 of them like every id in an address.
_ID = re.compile(r"[0-9]{1,18}", re.ASCII)
# A whole number in a form: ASCII digits, at most 18, a leading minus allowed (the domain refuses it then).
_NUMBER = re.compile(r"-?[0-9]{1,18}", re.ASCII)
INSPECTION_URL = "/inspection"


def kpi_cards(lang: str, rows: list[inspection.InspectionRow]) -> list[Kpi]:
    """Prints waiting and their units (Spec 10.1: only numbers the page has)."""
    return [
        Kpi("clipboard-check", "accent" if rows else "neutral", len(rows), translate(lang, "kpi.inspections_open")),
        Kpi("package", "neutral", sum(r.units for r in rows), translate(lang, "kpi.inspections_units")),
    ]


def _inspection_page(request: Request, db: Session, *, article: object = None,
                     error_inspection: int | None = None, error: str | None = None,
                     values: dict[str, str] | None = None, status: int = 200):
    """The list; `article` is the raw `article` value of the address or the form (the filter)."""
    flt = listing.article_filter(db, article, INSPECTION_URL)
    rows = inspection.list_open(db, article_id=flt.id if flt else None)
    return render(
        request,
        "inspection.html",
        status_code=status,
        active_nav="inspection",
        rows=rows,
        cards=kpi_cards(current_language(request), rows),
        article_filter=flt,
        error_inspection=error_inspection,
        error=error,
        values=values or {},
    )


def _scrap_value(raw: object) -> int | str:
    """The scrap of the form ("Ausschuss"): empty is 0 (Spec 8.2). Anything that is no whole number
    is handed on as it is. The range is checked in the domain, against the target number of the
    print: every wrong value gets that one text, "from 0 to the target number" (p93). A range
    checked here would be a second one (it was 1000, whatever the print)."""
    text = str(raw or "").strip()
    if not text:
        return 0
    return int(text) if _NUMBER.fullmatch(text) else text


def _back(article: object) -> str:
    """The list the action came from: with its filter, if it had one."""
    return listing.with_article(INSPECTION_URL, listing.article_id_of(article))


@router.get("/inspection")
async def inspection_page(request: Request, db: Session = Depends(get_db)):
    return _inspection_page(request, db, article=request.query_params.get("article"))


# The fixed path first: "all-good" must not be read as an id (the id route only takes digits).
@router.post("/inspection/all-good", dependencies=[Depends(verify_csrf)])
async def resolve_all_good(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    article = form.get("article")
    article_id = listing.article_id_of(article)
    # Words that are no id are dropped; the same id twice counts once.
    ids = list(dict.fromkeys(int(v) for v in form.getlist("ids") if _ID.fullmatch(str(v))))
    try:
        if not ids:
            return _inspection_page(
                request, db, article=article,
                error=translate(current_language(request), "inspections.none_selected"), status=200,
            )
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            # With a filter the server takes only the prints of that article, whatever ids came in.
            result = inspection.resolve_all_good(tx, ids, "user", article_id=article_id)
            messages = _shortages(tx, result.shortages)
    except FormTokenUsed:
        return duplicate_redirect(request, _back(article))
    except (DomainError, FormTokenInvalid) as exc:
        return _inspection_page(
            request, db, article=article, error=error_text(current_language(request), exc),
            status=error_status(exc),
        )
    flash(request, "inspections.all_done", resolved=result.resolved, skipped=result.skipped)
    for key, message_params in messages:
        flash(request, key, warning=key in WARNING_KEYS, **message_params)
    return RedirectResponse(_back(article), status_code=303)


@router.post("/inspection/{inspection_id:int}", dependencies=[Depends(verify_csrf)])
async def resolve_inspection(inspection_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    article = form.get("article")
    values = {k: str(v) for k, v in form.items() if k not in ("csrf_token", "form_token", "article")}
    try:
        # A page opened before the update still posts the old field `good`. Reading only `scrap`
        # (default 0) would book every unit as good and lose the scrap: refuse, book nothing.
        if "good" in form:
            raise ValidationError("inspections.form_outdated")
        # "Ausschuss", default 0 (Spec 8.2): good units are the units of the print minus the scrap.
        scrap = _scrap_value(form.get("scrap"))
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            result = inspection.resolve_scrap(tx, inspection_id, scrap, "user")
            name = tx.scalar(
                select(Article.name).join(Inspection, Inspection.article_id == Article.id)
                .where(Inspection.id == inspection_id)
            )
            messages = _shortages(tx, result.shortages)
    except FormTokenUsed:
        return duplicate_redirect(request, _back(article))
    except (DomainError, FormTokenInvalid) as exc:
        return _inspection_page(
            request, db, article=article, error_inspection=inspection_id,
            error=error_text(current_language(request), exc), values=values, status=error_status(exc),
        )
    flash(request, "inspections.done", name=name, good=result.good, scrap=result.scrap)
    if result.replacement_entry_id is not None:
        flash(request, "inspections.replacement_created")
    for key, message_params in messages:
        flash(request, key, warning=key in WARNING_KEYS, **message_params)
    return RedirectResponse(_back(article), status_code=303)


# ---- JSON interface (Spec 8.2): the same domain functions as the page, for a shelf display ----------


def _api_error(request: Request, exc: DomainError) -> JSONResponse:
    """`{"error": <key>, "message": <text in the user's language>}` with the status of the error."""
    if isinstance(exc, ValidationError):
        status = 422
    elif isinstance(exc, NotFoundError):
        status = 404
    elif isinstance(exc, ConflictError):
        status = 409
    else:  # e.g. a stock limit: the request conflicts with the stock as it is now
        status = 409
    return JSONResponse(
        {"error": exc.key, "message": error_text(current_language(request), exc)}, status_code=status
    )


def _counts(raw: bytes) -> tuple[int | None, int | str | None]:
    """`(good_units, scrap_units)` of a JSON body; one of them or both, whole numbers, nothing else.

    `scrap_units` is the input of the list (Spec 8.2: good = units of the print minus scrap);
    `good_units` stays for the displays that already send it. No `bool` (`true` is an int in
    Python), no `1.0`, no text: a display that sends the wrong type is told so instead of being
    guessed at. The range, and that both add up to the units, is checked against the print.
    A `scrap_units` of the wrong type is handed on as text for that check (the same text as the page).
    """
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):  # not JSON, not UTF-8, nested too deep
        raise ValidationError("inspections.invalid_body") from None
    if not isinstance(body, dict):
        raise ValidationError("inspections.invalid_body")
    if "good_units" not in body and "scrap_units" not in body:
        raise ValidationError("inspections.invalid_body")
    counts: list[int | None] = []
    for key, field in (("good_units", "inspections.field.good"), ("scrap_units", "inspections.field.scrap")):
        if key not in body:
            counts.append(None)
            continue
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, int):
            if key == "scrap_units":
                # Handed on as text, the domain refuses it with the range of the print: the same
                # text as on the page (p93). `None` stays "not sent" only when the key is missing.
                counts.append(str(value))
                continue
            raise ValidationError("form.invalid_number", field=field, min=0, max=MAX_GOOD)
        counts.append(value)
    return counts[0], counts[1]


@router.get("/api/inspections")
async def api_list(request: Request, db: Session = Depends(get_db)):
    raw_article = request.query_params.get("article")
    article_id = listing.article_id_of(raw_article)
    if raw_article and article_id is None:  # empty is no filter, like on the page
        return _api_error(request, ValidationError(
            "form.invalid_number", field="articles.title", min=1, max=MAX_QTY
        ))
    rows = inspection.list_open(db, article_id=article_id)
    return {
        "inspections": [
            {
                "id": r.id,
                "article_id": r.article_id,
                "article_name": r.article_name,
                "units": r.units,
                "needs_assembly": r.needs_assembly,
                # The database holds naive UTC; the interface says so.
                "created_at": r.created_at.replace(tzinfo=UTC).isoformat(),
            }
            for r in rows
        ]
    }


@router.post("/api/inspections/{inspection_id:int}", dependencies=[Depends(verify_csrf)])
async def api_resolve(inspection_id: int, request: Request):
    # Read before the write transaction: no awaiting inside it. A form body (token in a field
    # instead of the header) has been read by `verify_csrf` already and is no JSON anyway.
    try:
        raw = await request.body()
    except RuntimeError:
        raw = b""
    try:
        good, scrap = _counts(raw)
        with write_transaction(request) as tx:
            if scrap is None:
                result = inspection.resolve(tx, inspection_id, good, "user")
            else:
                # Not found and already inspected answer first (404, 409); only for a print that is
                # still open do the two counts have to add up.
                found = tx.execute(
                    select(Inspection.units, Inspection.resolved_at).where(Inspection.id == inspection_id)
                ).first()
                still_open = found is not None and found.resolved_at is None
                if (
                    still_open and good is not None
                    and isinstance(scrap, int) and good + scrap != found.units
                ):
                    raise ValidationError("inspections.counts_mismatch", units=found.units)
                result = inspection.resolve_scrap(tx, inspection_id, scrap, "user")
    except DomainError as exc:
        return _api_error(request, exc)
    return {
        "id": result.inspection_id,
        "good": result.good,
        "scrap": result.scrap,
        "replacement_entry_id": result.replacement_entry_id,
        "part_shortages": [{"part_id": s.part_id, "needed": s.needed, "had": s.had} for s in result.shortages],
    }


ASSEMBLY_URL = "/assembly"


def _assembly_page(request: Request, db: Session, *, article: object = None,
                   error_article: int | None = None, error: str | None = None,
                   values: dict[str, str] | None = None, status: int = 200):
    """The list; `article` is the raw `article` value of the address or the form (the filter)."""
    flt = listing.article_filter(db, article, ASSEMBLY_URL)
    return render(
        request,
        "assembly.html",
        status_code=status,
        active_nav="assembly",
        rows=inspection.list_awaiting_assembly(db, article_id=flt.id if flt else None),
        article_filter=flt,
        error_article=error_article,
        error=error,
        values=values or {},
    )


@router.get("/assembly")
async def assembly_page(request: Request, db: Session = Depends(get_db)):
    return _assembly_page(request, db, article=request.query_params.get("article"))


@router.post("/assembly/{article_id:int}", dependencies=[Depends(verify_csrf)])
async def book_assembly(article_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    article = form.get("article")
    back = listing.with_article(ASSEMBLY_URL, listing.article_id_of(article))
    values = {k: str(v) for k, v in form.items() if k not in ("csrf_token", "form_token", "article")}
    try:
        qty = parse_int(form.get("qty"), field="bookings.field.qty", min=1, max=MAX_QTY)
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            article_row = _active_article(tx, article_id)
            name = article_row.name
            shortages = inspection.assemble(tx, article_id, qty, "user")
            messages = _shortages(tx, shortages)
    except FormTokenUsed:
        return duplicate_redirect(request, back)
    except (DomainError, FormTokenInvalid) as exc:
        return _assembly_page(
            request, db, article=article, error_article=article_id,
            error=error_text(current_language(request), exc), values=values, status=error_status(exc),
        )
    flash(request, "assembly.done", name=name, n=qty)
    for key, message_params in messages:
        flash(request, key, warning=key in WARNING_KEYS, **message_params)
    return RedirectResponse(back, status_code=303)
