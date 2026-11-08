"""Labels with the QR code of an article on a Brother label printer (Spec 8.5).

The settings (printer address, roll, chosen details) and the trial print are on the settings page,
the preview and the print on the article's page. The printer address comes from the stored
settings only (or, for the trial print, from what is typed in the settings form and checked the
same way), never from anything else in a request.

The network part is blocking and slow (the printer answers in its own time): it runs in a worker
thread, after the database connection of the request is handed back, so neither the event loop nor
a database lock waits for the printer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from reprintbuddy import settings as st
from reprintbuddy.labels import fields, service
from reprintbuddy.labels.addresses import is_own
from reprintbuddy.labels.rolls import ROLLS
from reprintbuddy.models import Article
from reprintbuddy.web import labels_view
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, release_connection
from reprintbuddy.web.flash import flash
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction
from reprintbuddy.web.forms import duplicate_redirect
from reprintbuddy.web.routes.ops import ops_ready
from reprintbuddy.web.routes.settings import SECTION_TAB, _page, _saved

router = APIRouter()

_NO_STORE = "no-store"
SETTINGS_PAGE = "/settings?tab=labels"


def _picture_response(rendered) -> Response:
    return Response(
        labels_view.png(rendered.image),
        media_type="image/png",
        headers={"Cache-Control": _NO_STORE, "X-Label-Cut": ",".join(str(i) for i in rendered.truncated)},
    )


def _form_values(form) -> tuple[str, str, list[str]]:
    """What the label form of the settings holds: address (as typed), roll, ticked details."""
    return (
        str(form.get("label_printer_host", "")).strip(),
        str(form.get("label_roll", "")),
        [str(item) for item in form.getlist("label_field")],
    )


def _checked(host: str, roll: str, chosen: list[str], own: frozenset[str]) -> tuple[object, str, list[str]] | str:
    """The three values as the settings would store them, or the translation key of the problem.
    `own` are this server's addresses: the printer is another device."""
    try:
        checked_host = st.validate("label_printer_host", host or None)
    except ValueError:
        return "settings.invalid_label_host"
    if isinstance(checked_host, str) and is_own(checked_host, own):
        return "settings.invalid_label_host"
    try:
        checked_roll = str(st.validate("label_roll", roll))
    except ValueError:
        return "settings.invalid_label_roll"
    try:
        checked_fields = list(st.validate("label_fields", chosen))  # type: ignore[arg-type]
    except ValueError:
        return "settings.invalid_label_fields"
    return checked_host, checked_roll, checked_fields


# ---- settings: save, preview, trial print ----------------------------------------------------


@router.post("/settings/labels", dependencies=[Depends(verify_csrf)])
async def save_labels(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    host, roll, chosen = _form_values(form)
    typed = {"label_host": host, "label_roll": roll, "label_selected": list(fields.clean(chosen))}
    result = _checked(host, roll, chosen, labels_view.own_addresses(request))
    if isinstance(result, str):
        return _page(request, db, result, "labels", busy=await ops_ready(request), **typed)
    checked_host, checked_roll, checked_fields = result
    with write_transaction(request) as tx:  # all three or none
        st.set_setting(tx, "label_printer_host", checked_host, commit=False)
        st.set_setting(tx, "label_roll", checked_roll, commit=False)
        st.set_setting(tx, "label_fields", checked_fields, commit=False)
    return _saved(request, tab=SECTION_TAB["labels"])


@router.get("/settings/labels/preview.png")
async def settings_preview(request: Request, fields_: str | None = Query(None, alias="fields"), db: Session = Depends(get_db)):
    """A sample label with the saved details, or with the ones in `fields` (the ticks of the form)."""
    lang = current_language(request)
    saved = labels_view.LabelSettings(db)
    chosen = saved.fields if fields_ is None else fields.clean(fields_.split(","))
    address = labels_view.label_address(request, db, labels_view.SAMPLE_ARTICLE_ID)
    rendered = await run_in_threadpool(
        labels_view.draw, lang, labels_view.sample_data(lang), chosen, address, saved.roll
    )
    return _picture_response(rendered)


@router.post("/settings/labels/test", dependencies=[Depends(verify_csrf)])
async def trial_print(request: Request, db: Session = Depends(get_db)):
    """One sample label with what is typed and ticked in the form, without saving it. The page is
    shown again with what was typed and the message (a redirect would lose the unsaved choice)."""
    form = await request.form()
    host, roll_key, chosen = _form_values(form)
    typed = {"label_host": host, "label_roll": roll_key, "label_selected": list(fields.clean(chosen))}
    result = _checked(host, roll_key, chosen, labels_view.own_addresses(request))
    if isinstance(result, str):
        return _page(request, db, result, "labels", busy=await ops_ready(request), **typed)
    checked_host, checked_roll, checked_fields = result
    if checked_host is None:
        flash(request, "labels.not_configured", warning=True)
        return _page(request, db, None, "labels", busy=await ops_ready(request), **typed)
    try:  # the one-time token is used up first and committed: the printing below holds no lock
        with form_transaction(request, str(form.get("form_token", ""))):
            pass
    except FormTokenUsed:
        flash(request, "form.duplicate")
        return _page(request, db, None, "labels", busy=await ops_ready(request), **typed)
    except FormTokenInvalid:
        flash(request, "form.expired", warning=True)
        return _page(request, db, None, "labels", busy=await ops_ready(request), **typed)
    lang = current_language(request)
    roll = ROLLS[checked_roll]
    address = labels_view.label_address(request, db, labels_view.SAMPLE_ARTICLE_ID)
    rendered = await run_in_threadpool(
        labels_view.draw, lang, labels_view.sample_data(lang), checked_fields, address, roll
    )
    await _print(request, db, lang, str(checked_host), roll, rendered.image, 1)
    return _page(request, db, None, "labels", busy=await ops_ready(request), **typed)


# ---- the article's page: preview and print --------------------------------------------------


def _article_or_404(db: Session, article_id: int) -> Article:
    article = db.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404)
    return article


@router.get("/articles/{article_id:int}/label.png")
async def article_preview(request: Request, article_id: int, db: Session = Depends(get_db)):
    """The label as it would be printed now: the article's details as chosen in the settings."""
    article = _article_or_404(db, article_id)
    lang = current_language(request)
    saved = labels_view.LabelSettings(db)
    address = labels_view.label_address(request, db, article.id)
    data = labels_view.article_data(article, lang)
    rendered = await run_in_threadpool(labels_view.draw, lang, data, saved.fields, address, saved.roll)
    return _picture_response(rendered)


def _copies(raw: object) -> int | None:
    text = str(raw or "").strip()
    if not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if 1 <= number <= service.MAX_COPIES else None


@router.post("/articles/{article_id:int}/label", dependencies=[Depends(verify_csrf)])
async def print_article_label(request: Request, article_id: int, db: Session = Depends(get_db)):
    article = _article_or_404(db, article_id)
    back = f"/articles/{article_id}"
    form = await request.form()
    copies = _copies(form.get("copies"))
    if copies is None:
        flash(request, "labels.copies_invalid", warning=True, max=service.MAX_COPIES)
        return RedirectResponse(back, status_code=303)
    saved = labels_view.LabelSettings(db)
    if saved.host is None:
        flash(request, "labels.not_configured", warning=True)
        return RedirectResponse(back, status_code=303)
    try:  # the one-time token is used up first and committed: the printing below holds no lock
        with form_transaction(request, str(form.get("form_token", ""))):
            pass
    except FormTokenUsed:
        return duplicate_redirect(request, back)
    except FormTokenInvalid:
        flash(request, "form.expired", warning=True)
        return RedirectResponse(back, status_code=303)
    lang = current_language(request)
    address = labels_view.label_address(request, db, article.id)
    data = labels_view.article_data(article, lang)
    rendered = await run_in_threadpool(labels_view.draw, lang, data, saved.fields, address, saved.roll)
    await _print(request, db, lang, saved.host, saved.roll, rendered.image, copies)
    return RedirectResponse(back, status_code=303)


async def _print(request: Request, db: Session, lang: str, host: str, roll, image, copies: int) -> None:
    """Print in a worker thread and flash what happened."""
    release_connection(db)  # nothing may hold the database while the printer takes its time
    outcome = await run_in_threadpool(
        service.print_labels, host, roll, image, copies, refuse=labels_view.own_addresses(request)
    )
    key, params, warning = labels_view.outcome_message(lang, outcome, roll)
    flash(request, key, warning=warning, **params)
