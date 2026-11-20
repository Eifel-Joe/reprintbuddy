"""Routes for inspection and assembly (Spec 8.2, 8.3): the page "To check" and the assembly page."""

from __future__ import annotations

import json
import re
from datetime import UTC

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import assembled, inspection
from reprintbuddy.domain.errors import ConflictError, DomainError, NotFoundError, ValidationError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Article, Inspection
from reprintbuddy.web import listing
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.flash import flash
from reprintbuddy.web.forms import error_text, parse_int, run_form_action
from reprintbuddy.web.formtoken import write_transaction
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.kpis import Kpi
from reprintbuddy.web.routes.bookings import WARNING_KEYS, _active_article, _shortages

router = APIRouter()

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
                     error_inspection: int | None = None, error_article: int | None = None,
                     error: str | None = None, values: dict[str, str] | None = None, status: int = 200):
    """The list, one row per article (Spec 8.2, p713); `article` is the raw `article` value of the
    address or the form (the filter). An error belongs to a row (`error_article`), to an unfolded
    print (`error_inspection`) or, if neither is shown, to the top of the list."""
    flt = listing.article_filter(db, article, INSPECTION_URL)
    groups = inspection.list_groups(db, article_id=flt.id if flt else None)
    return render(
        request,
        "inspection.html",
        status_code=status,
        active_nav="inspection",
        groups=groups,
        cards=kpi_cards(current_language(request), [p for g in groups for p in g.prints]),
        article_filter=flt,
        error_inspection=error_inspection,
        error_article=error_article,
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


def _good_value(raw: object) -> int | str:
    """The field "good" (Spec 8.2, p713): a whole number, or the text as it came (empty too).

    The domain refuses anything that is no whole number from 0 to the target number with that one
    text, "from 0 to the target number" (p93). Empty is no answer (p713 F1): it could mean none or
    all, so it is refused like any other text, not read as either."""
    text = str(raw or "").strip()
    return int(text) if _NUMBER.fullmatch(text) else text


def _picked_ids(form) -> list[int]:
    """The prints of "Selected all good" / "All good": the ticked rows (`group`, the ids of an
    article row joined by spaces, p713) and single ids (`ids`, "All good" and pages from before
    the update). Words that are no id are dropped; the same id twice counts once."""
    words = [str(v) for v in form.getlist("ids")]
    for group in form.getlist("group"):
        words += str(group).split()
    return list(dict.fromkeys(int(w) for w in words if _ID.fullmatch(w)))


def _flash_replacements(request: Request, made: int, scrap: int) -> None:
    """One message on the replacement of a confirmed row or print (p713 F5; p411, Spec 7.5)."""
    if made == 1:
        flash(request, "inspections.replacement_created")
    elif made > 1:
        flash(request, "inspections.replacements_created", n=made)
    elif scrap > 0:  # the target is reached without it
        flash(request, "inspections.replacement_not_needed")


def _back(article: object) -> str:
    """The list the action came from: with its filter, if it had one."""
    return listing.with_article(INSPECTION_URL, listing.article_id_of(article))


def _with_shortages(request: Request, messages: list[tuple[str, dict]], url: str) -> RedirectResponse:
    """Back to `url` with the shortage messages of a booking (warnings among them as warnings)."""
    for key, message_params in messages:
        flash(request, key, warning=key in WARNING_KEYS, **message_params)
    return RedirectResponse(url, status_code=303)


@router.get("/inspection")
async def inspection_page(request: Request, db: Session = Depends(get_db)):
    return _inspection_page(request, db, article=request.query_params.get("article"))


# The fixed path first: "all-good" must not be read as an id (the id route only takes digits).
@router.post("/inspection/all-good", dependencies=[Depends(verify_csrf)])
async def resolve_all_good(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    article = form.get("article")
    article_id = listing.article_id_of(article)
    ids = _picked_ids(form)
    if not ids:
        return _inspection_page(
            request, db, article=article,
            error=translate(current_language(request), "inspections.none_selected"), status=200,
        )

    def save(tx):
        # With a filter the server takes only the prints of that article, whatever ids came in.
        result = inspection.resolve_all_good(tx, ids, "user", article_id=article_id)
        return result, _shortages(tx, result.shortages)

    def done(outcome):
        result, messages = outcome
        flash(request, "inspections.all_done", resolved=result.resolved, skipped=result.skipped)
        return _with_shortages(request, messages, _back(article))

    return await run_form_action(
        request, work=save, done=done, duplicate_url=_back(article),
        on_error=lambda text, status, exc: _inspection_page(request, db, article=article, error=text, status=status),
    )


@router.post("/inspection/articles/{article_id:int}", dependencies=[Depends(verify_csrf)])
async def resolve_article(article_id: int, request: Request, db: Session = Depends(get_db)):
    """The row of an article (Spec 8.2, p713): the good pieces of the prints the row showed."""
    form = await request.form()
    article = form.get("article")
    ids = [int(v) for v in form.getlist("ids") if _ID.fullmatch(str(v))]
    values = {"good": str(form.get("good") or "")}

    def save(tx, good):
        result = inspection.resolve_group(tx, article_id, ids, good, "user")
        return result, tx.get(Article, article_id).name, _shortages(tx, result.shortages)

    def done(outcome):
        result, name, messages = outcome
        if len(result.prints) == 1:
            flash(request, "inspections.done", name=name, good=result.good, scrap=result.scrap)
        else:
            flash(request, "inspections.group_done", name=name, good=result.good, scrap=result.scrap,
                  prints=len(result.prints))
        _flash_replacements(request, result.replacements, result.scrap)
        return _with_shortages(request, messages, _back(article))

    return await run_form_action(
        request, check=lambda: _good_value(form.get("good")), work=save, done=done,
        duplicate_url=_back(article),
        on_error=lambda text, status, exc: _inspection_page(
            request, db, article=article, error_article=article_id, error=text, values=values, status=status
        ),
    )


@router.post("/inspection/{inspection_id:int}", dependencies=[Depends(verify_csrf)])
async def resolve_inspection(inspection_id: int, request: Request, db: Session = Depends(get_db)):
    """One print, unfolded under its article: its good pieces (Spec 8.2, p713)."""
    form = await request.form()
    article = form.get("article")
    values = {k: str(v) for k, v in form.items() if k in ("good", "scrap")}

    def check():
        # p713 F7: "good" is the field of the page; a page opened before the update still posts the
        # scrap, which is just as clear (good = units − scrap), so it is booked, not refused.
        if "good" not in form and "scrap" in form:
            return "scrap", _scrap_value(form.get("scrap"))
        return "good", _good_value(form.get("good"))

    def save(tx, checked):
        kind, n = checked
        if kind == "scrap":
            result = inspection.resolve_scrap(tx, inspection_id, n, "user")
        else:
            result = inspection.resolve(tx, inspection_id, n, "user")
        name = tx.scalar(
            select(Article.name).join(Inspection, Inspection.article_id == Article.id)
            .where(Inspection.id == inspection_id)
        )
        return result, name, _shortages(tx, result.shortages)

    def done(outcome):
        result, name, messages = outcome
        flash(request, "inspections.done", name=name, good=result.good, scrap=result.scrap)
        _flash_replacements(request, int(result.replacement_entry_id is not None), result.scrap)
        return _with_shortages(request, messages, _back(article))

    return await run_form_action(
        request, check=check, work=save, done=done, duplicate_url=_back(article),
        on_error=lambda text, status, exc: _inspection_page(
            request, db, article=article, error_inspection=inspection_id, error=text, values=values, status=status
        ),
    )


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


def _counts(raw: bytes) -> tuple[int | str | None, int | str | None]:
    """`(good_units, scrap_units)` of a JSON body; one of them or both, whole numbers, nothing else.

    `scrap_units` is the input of the list (Spec 8.2: good = units of the print minus scrap);
    `good_units` stays for the displays that already send it. No `bool` (`true` is an int in
    Python), no `1.0`, no text: a display that sends the wrong type is told so instead of being
    guessed at. The range, and that both add up to the units, is checked against the print.
    A count of the wrong type is handed on as text for that check (the same text as the page).
    """
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):  # not JSON, not UTF-8, nested too deep
        raise ValidationError("inspections.invalid_body") from None
    if not isinstance(body, dict):
        raise ValidationError("inspections.invalid_body")
    if "good_units" not in body and "scrap_units" not in body:
        raise ValidationError("inspections.invalid_body")
    counts: list[int | str | None] = []
    for key in ("good_units", "scrap_units"):
        if key not in body:
            counts.append(None)
            continue
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, int):
            # Handed on as text, the domain refuses it with the range of the print: the same text as on
            # the page (p93; `good_units` too since v0.3.14 p181, it said "0 to 1000" before).
            # `None` stays "not sent" only when the key is missing.
            counts.append(str(value))
            continue
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
    groups = inspection.list_groups(db, article_id=article_id)
    return {
        "inspections": [
            {
                "id": r.id,
                "article_id": r.article_id,
                "article_name": r.article_name,
                "units": r.units,
                "needs_assembly": r.needs_assembly,
                # The database holds naive UTC; the interface says so.
                "created_at": _utc(r.created_at),
            }
            for r in sorted((p for g in groups for p in g.prints), key=lambda r: (r.created_at, r.id))
        ],
        # p713 (Spec 8.2): the rows of the page, one per article, for a display that books like the browser.
        "articles": [
            {
                "article_id": g.article_id,
                "article_name": g.article_name,
                "needs_assembly": g.needs_assembly,
                "units": g.units,
                "inspection_ids": g.ids,
                "finished_at": [_utc(p.created_at) for p in g.prints],
            }
            for g in groups
        ],
    }


def _utc(at) -> str:
    return at.replace(tzinfo=UTC).isoformat()


def _group_body(raw: bytes) -> tuple[list[int], int | str]:
    """`(inspection_ids, good_units)` of a JSON body for the row of an article (p713).

    Both are needed: the ids are the prints the display showed, so a print reported after it drew
    the row is not booked unseen (as on the page). Ids are whole numbers (no bool, no 1.0, no text)
    and at least one; a count of the wrong type is handed on as text, so the domain refuses it with
    the range of the row (the same text as on the page)."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValidationError("inspections.invalid_group_body") from None
    if not isinstance(body, dict) or "good_units" not in body:
        raise ValidationError("inspections.invalid_group_body")
    ids = body.get("inspection_ids")
    if (
        not isinstance(ids, list) or not ids
        or any(isinstance(i, bool) or not isinstance(i, int) for i in ids)
    ):
        raise ValidationError("inspections.invalid_group_body")
    good = body["good_units"]
    if isinstance(good, bool) or not isinstance(good, int):
        good = str(good)
    return ids, good


@router.post("/api/inspections/articles/{article_id:int}", dependencies=[Depends(verify_csrf)])
async def api_resolve_article(article_id: int, request: Request):
    """The row of an article by its good pieces: the same domain function as the page (Spec 8.2)."""
    try:
        raw = await request.body()
    except RuntimeError:
        raw = b""
    try:
        ids, good = _group_body(raw)
        with write_transaction(request) as tx:
            result = inspection.resolve_group(tx, article_id, ids, good, "user")
    except DomainError as exc:
        return _api_error(request, exc)
    return {
        "article_id": result.article_id,
        "good": result.good,
        "scrap": result.scrap,
        "prints": [
            {"id": p.inspection_id, "good": p.good, "scrap": p.scrap, "replacement_entry_id": p.replacement_entry_id}
            for p in result.prints
        ],
        "replacement_entry_ids": [p.replacement_entry_id for p in result.prints if p.replacement_entry_id is not None],
        "part_shortages": [{"part_id": s.part_id, "needed": s.needed, "had": s.had} for s in result.shortages],
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
                if still_open and good is not None and not isinstance(good, int):
                    # The old field next to the scrap: a wrong type is refused, not ignored.
                    raise ValidationError(
                        "form.invalid_number", field="inspections.field.good", min=0, max=found.units
                    )
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
        # 0.4.0 (p341): variants on stock below their target, and open orders built on order
        build_rows=assembled.list_buildable(db, article_id=flt.id if flt else None),
        order_rows=assembled.list_for_orders(db, article_id=flt.id if flt else None),
        article_filter=flt,
        error_article=error_article,
        error=error,
        values=values or {},
    )


@router.get("/assembly")
async def assembly_page(request: Request, db: Session = Depends(get_db)):
    return _assembly_page(request, db, article=request.query_params.get("article"))


@router.post("/assembly/build/{holder_id:int}", dependencies=[Depends(verify_csrf)])
async def build_from_parts(holder_id: int, request: Request, db: Session = Depends(get_db)):
    """Montage aus Teilen of one variant (Spec 8.3): from the list or from the article's page."""
    form = await request.form()
    article = form.get("article")
    next_url = str(form.get("next", ""))
    back = next_url if next_url.startswith("/articles/") else listing.with_article(ASSEMBLY_URL, listing.article_id_of(article))
    values = {k: str(v) for k, v in form.items() if k not in ("csrf_token", "form_token", "article", "next")}

    def save(tx, qty):
        holder = tx.get(Article, holder_id)
        label = (holder.sku or holder.name) if holder is not None else ""
        shortages = assembled.build(tx, holder_id, qty, "user")
        return label, qty, _shortages(tx, shortages)

    def done(outcome):
        label, qty, messages = outcome
        flash(request, "assembly.built", name=label, n=qty)
        return _with_shortages(request, messages, back)

    def refused(text, status, exc):
        if next_url.startswith("/articles/"):  # from the article's page: back there, as a warning
            flash(request, "assembly.build_refused", warning=True, text=text)
            return RedirectResponse(back, status_code=303)
        return _assembly_page(
            request, db, article=article, error_article=holder_id, error=text, values=values, status=status
        )

    return await run_form_action(
        request, check=lambda: parse_int(form.get("qty"), field="bookings.field.qty", min=1, max=MAX_QTY),
        work=save, done=done, duplicate_url=back, on_error=refused,
    )


@router.post("/assembly/{article_id:int}", dependencies=[Depends(verify_csrf)])
async def book_assembly(article_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    article = form.get("article")
    back = listing.with_article(ASSEMBLY_URL, listing.article_id_of(article))
    values = {k: str(v) for k, v in form.items() if k not in ("csrf_token", "form_token", "article")}

    def save(tx, qty):
        name = _active_article(tx, article_id).name
        shortages = inspection.assemble(tx, article_id, qty, "user")
        return name, qty, _shortages(tx, shortages)

    def done(outcome):
        name, qty, messages = outcome
        flash(request, "assembly.done", name=name, n=qty)
        return _with_shortages(request, messages, back)

    return await run_form_action(
        request, check=lambda: parse_int(form.get("qty"), field="bookings.field.qty", min=1, max=MAX_QTY),
        work=save, done=done, duplicate_url=back,
        on_error=lambda text, status, exc: _assembly_page(
            request, db, article=article, error_article=article_id, error=text, values=values, status=status
        ),
    )
