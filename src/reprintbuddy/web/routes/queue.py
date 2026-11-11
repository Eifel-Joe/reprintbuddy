"""The queue page (Spec 7.2, 7.4, 7.8, 10.3; Design 2026-11-06 sections 4 and 5; plan B4 Task 12).

Reprintbuddy's own queue (dragging through static/js/queue-drag.js; a menu per row that works
as plain forms without JavaScript), manual prints, prints in progress, a read-only view of
Bambuddy's queue and the warnings. Every part is a live region.

Bambuddy's queue: the whole page (a GET without `X-Live-Regions`) asks Bambuddy afresh, keeps
the answer in the engine status and tells the open pages (topic `bambuddy`) only when the list
changed. A region request reads the status only: asking Bambuddy there would publish, which
makes every open page fetch its regions again, which would ask Bambuddy again.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import QUEUE_STATUSES
from reprintbuddy.domain import dispatch, workqueue
from reprintbuddy.domain import articlesets
from reprintbuddy.domain.errors import DomainError, ValidationError
from reprintbuddy.models import Article, Dispatch, WorkQueueEntry
from reprintbuddy.web import regions
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, db_session, get_bambuddy, render
from reprintbuddy.web.engine_view import warnings_context
from reprintbuddy.web.flash import flash
from reprintbuddy.web.forms import (
    bounded_int,
    duplicate_redirect,
    error_status,
    error_text,
    parse_int,
    saved_redirect,
)
from reprintbuddy.web.formtoken import (
    FormTokenInvalid,
    FormTokenUsed,
    form_transaction,
    write_transaction,
)
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.kpis import Kpi

log = logging.getLogger(__name__)
router = APIRouter()

QUEUE_URL = "/queue"
# Where an entry may go: only these words reach the domain (a stray text would end in a
# ValueError there, a server error). Ids have at most 18 digits like every id in an address.
MOVE_TO = re.compile(r"(top|bottom)|before:([0-9]{1,18})", re.ASCII)
WHERE = re.compile(r"(start|end)|before:([0-9]{1,18})", re.ASCII)
# The drag script asks with this header and gets a status instead of a page.
FETCH_HEADER = "X-Requested-With"
FETCH_VALUE = "fetch"
# Badge colour of an open print (the in-progress table shows only open ones).
DISPATCH_TONES = {"sending": "warn", "unclear": "bad", "queued": "info", "printing": "ok"}
MANUAL_DEFAULTS = {"article_id": "", "prints": "1", "where": "end"}


# ---- Bambuddy's queue ----------------------------------------------------------------------


async def _refresh_bambuddy_queue(request: Request) -> bool:
    """Ask Bambuddy for its queue (whole page only); True when it answered.

    Runs before the page reads the database, so no transaction waits on the network.
    """
    client = get_bambuddy(request)
    if client is None:
        return False
    ticker = request.app.state.ticker
    status = ticker.status
    try:
        items = await client.list_queue()
    except (BambuddyUnavailable, BambuddyRejected) as exc:
        log.info("queue page: Bambuddy's queue could not be read: %s", exc)
        # Open pages learn once that their list is no longer current.
        if status.bambuddy_queue_ok:
            status.bambuddy_queue_ok = False
            request.app.state.live.publish(frozenset({"bambuddy"}))
        return False
    changed = status.bambuddy_queue != items or not status.bambuddy_queue_ok
    status.bambuddy_queue = items
    status.bambuddy_queue_at = ticker.now()
    status.bambuddy_queue_ok = True
    if changed:
        request.app.state.live.publish(frozenset({"bambuddy"}))
    return True


# ---- the page ------------------------------------------------------------------------------


def kpi_cards(t: Callable[[str], str], entries: list[workqueue.EntryRow], open_prints: int) -> list[Kpi]:
    """Prints waiting and prints in Bambuddy always; held entries and paused articles when
    there are any (Spec 10.1: the cards count only what is there)."""
    waiting = sum(e.prints for e in entries if not e.held and not e.article_paused)
    held = sum(1 for e in entries if e.held)
    paused = len({e.article_id for e in entries if e.article_paused})
    cards = [
        Kpi("clock", "accent", waiting, t("queue.kpi.waiting")),
        Kpi("printer", "ok", open_prints, t("queue.kpi.in_bambuddy")),
    ]
    if held:
        cards.append(Kpi("pause", "neutral", held, t("queue.kpi.held")))
    if paused:
        cards.append(Kpi("pause", "neutral", paused, t("queue.kpi.paused")))
    return cards


def _bambuddy_view(request: Request, db: Session, fresh: bool | None) -> dict[str, object]:
    status = request.app.state.ticker.status
    items = status.bambuddy_queue
    if fresh is None:  # a region or the answer to a form: the last read of the list
        fresh = status.configured and status.bambuddy_queue_ok
    own_ids = dispatch.own_item_ids(db)
    username = st.get_setting(db, "bambuddy_username")
    rows = [
        (item, item.id in own_ids or (bool(username) and item.created_by == username))
        for item in (items or [])
    ]
    return {
        "bambuddy_rows": rows,
        "bambuddy_foreign_ahead": dispatch.foreign_ahead(rows),
        "bambuddy_states": QUEUE_STATUSES,
        "bambuddy_known": items is not None,
        "bambuddy_fresh": fresh,
        "bambuddy_at": status.bambuddy_queue_at,
    }


def _page(
    request: Request,
    *,
    bambuddy_fresh: bool | None = None,
    error: str | None = None,
    error_for: str | None = None,
    manual_values: dict[str, str] | None = None,
    status_code: int = 200,
):
    db = db_session(request)
    lang = current_language(request)
    entries = workqueue.list_entries(db)
    ids = {e.article_id for e in entries}
    thumbs: set[int] = set()
    reasons: dict[int, str | None] = {}
    if ids:
        for article_id, has_thumb, reason in db.execute(
            select(Article.id, Article.thumbnail.is_not(None), Article.pause_reason).where(Article.id.in_(ids))
        ):
            if has_thumb:
                thumbs.add(article_id)
            reasons[article_id] = reason
    in_progress = db.execute(
        select(Dispatch, Article.name)
        .join(Article, Article.id == Dispatch.article_id)
        .where(Dispatch.status.in_(workqueue.OPEN_STATES))
        .order_by(Dispatch.id)
    ).all()
    articles = db.execute(
        select(Article.id, Article.name).where(articlesets.where_print()).order_by(Article.name, Article.id)
    ).all()
    return render(
        request,
        "queue.html",
        status_code=status_code,
        active_nav="queue",
        entries=entries,
        thumbs=thumbs,
        pause_reasons=reasons,
        cards=kpi_cards(lambda key: translate(lang, key), entries, len(in_progress)),
        in_progress=in_progress,
        dispatch_tones=DISPATCH_TONES,
        articles=articles,
        manual=manual_values or dict(MANUAL_DEFAULTS),
        max_prints=workqueue.MAX_PRINTS,  # the limit of the prints fields
        poll_now=get_bambuddy(request) is not None,  # the button needs a set-up Bambuddy
        error=error,
        error_for=error_for,
        **_bambuddy_view(request, db, bambuddy_fresh),
        **warnings_context(request, db),
    )


@router.get(QUEUE_URL)
async def queue_page(request: Request):
    fresh = None
    if not regions.requested(request):
        fresh = await _refresh_bambuddy_queue(request)
    return _page(request, bambuddy_fresh=fresh)


# ---- query Bambuddy now (v0.3.6 p87) --------------------------------------------------------

# The pages that carry the button; `next` may name only these (never an address from outside).
POLL_RETURN_PAGES = frozenset({QUEUE_URL, "/"})


@router.post("/queue/poll", dependencies=[Depends(verify_csrf)])
async def queue_poll(request: Request):
    """Ask the tick engine for a regular poll now (`Engine.request_poll`).

    That only sets a flag: the engine's own loop polls, so a second click while a poll is asked
    for merges with it, and the result reaches the open pages live (topic `bambuddy`). The
    one-time token turns a double submit of the same form into "already done". Paused while an
    update runs by the update guard, like every other write. Without a set-up Bambuddy nothing
    is asked for.
    """
    form = await request.form()
    target = str(form.get("next", ""))
    target = target if target in POLL_RETURN_PAGES else QUEUE_URL
    if get_bambuddy(request) is None:
        flash(request, "queue.poll_unavailable", warning=True)
        return RedirectResponse(target, status_code=303)
    try:
        with form_transaction(request, str(form.get("form_token", ""))):
            pass  # nothing is written; the token is used up
    except FormTokenUsed:
        return duplicate_redirect(request, target)
    except FormTokenInvalid:
        # An expired or forged form: back to the page of the button (not the queue page under
        # this address), with the usual notice.
        flash(request, "form.expired", warning=True)
        return RedirectResponse(target, status_code=303)
    request.app.state.ticker.request_poll()
    return saved_redirect(request, target, "queue.poll_requested")


# ---- manual prints (Spec 7.4) ----------------------------------------------------------------


def _where(raw: str) -> str | int:
    match = WHERE.fullmatch(raw)
    if match is None:
        raise ValidationError("queue.invalid_where")
    return match.group(1) or int(match.group(2))


@router.post("/queue/manual", dependencies=[Depends(verify_csrf)])
async def queue_manual(request: Request):
    form = await request.form()
    values = {name: str(form.get(name, "")) for name in MANUAL_DEFAULTS}
    try:
        article_id = bounded_int(values["article_id"].strip())
        if article_id is None:
            raise ValidationError("form.required", field="queue.field.article")
        prints = parse_int(values["prints"], field="queue.field.prints", min=1, max=workqueue.MAX_PRINTS)
        where = _where(values["where"] or "end")  # the default of Spec 7.4
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            workqueue.add_entry(tx, article_id, prints, "manual", "user", where=where)
            name = tx.get(Article, article_id).name
    except FormTokenUsed:
        return duplicate_redirect(request, QUEUE_URL)
    except (DomainError, FormTokenInvalid) as exc:
        return _page(
            request,
            error=error_text(current_language(request), exc),
            error_for="manual",
            manual_values=values,
            status_code=error_status(exc),
        )
    return saved_redirect(request, QUEUE_URL, "queue.created", name=name, prints=prints)


# ---- moving (Spec 7.2, Design 5) -------------------------------------------------------------


@router.post("/queue/{entry_id:int}/move", dependencies=[Depends(verify_csrf)])
async def queue_move(request: Request, entry_id: int):
    """`to` = top | bottom | before:<id>. No one-time token: moving can be repeated and writes
    no journal line. The drag script (`X-Requested-With: fetch`) gets 204, 400 or 409 with a
    short text; a form gets the page."""
    form = await request.form()
    by_fetch = request.headers.get(FETCH_HEADER) == FETCH_VALUE
    match = MOVE_TO.fullmatch(str(form.get("to", "")))
    if match is None:
        text = translate(current_language(request), "queue.invalid_move")
        return PlainTextResponse(text, status_code=400) if by_fetch else _page(request, error=text)
    to: str | int = match.group(1) or int(match.group(2))
    try:
        with write_transaction(request) as tx:
            workqueue.move_entry(tx, entry_id, to)  # type: ignore[arg-type]
    except DomainError as exc:  # the entry or the target is gone (another device)
        text = error_text(current_language(request), exc)
        return PlainTextResponse(text, status_code=409) if by_fetch else _page(request, error=text)
    if by_fetch:
        return Response(status_code=204)
    return RedirectResponse(QUEUE_URL, status_code=303)


# ---- hold, release, prints, delete ------------------------------------------------------------

Message = tuple[str, bool, dict[str, object]]  # key, warning, parameters


def _article_of(tx: Session, entry_id: int) -> Article | None:
    return tx.scalar(
        select(Article).join(WorkQueueEntry, WorkQueueEntry.article_id == Article.id).where(WorkQueueEntry.id == entry_id)
    )


def _paused_now(article: Article | None, was_paused: bool) -> list[Message]:
    """The pause a change made (Spec 7.2: fewer prints or deleting a rule entry), as a message."""
    if article is None or was_paused or not article.paused or not article.pause_reason:
        return []
    return [(f"pause.reason.{article.pause_reason}", True, {})]


async def _entry_action(request: Request, work: Callable[[Session, Any], list[Message]]):
    form = await request.form()
    try:
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            messages = work(tx, form)
    except FormTokenUsed:
        return duplicate_redirect(request, QUEUE_URL)
    except (DomainError, FormTokenInvalid) as exc:
        return _page(request, error=error_text(current_language(request), exc), status_code=error_status(exc))
    for key, warning, params in messages:
        flash(request, key, warning=warning, **params)
    return RedirectResponse(QUEUE_URL, status_code=303)


@router.post("/queue/{entry_id:int}/hold", dependencies=[Depends(verify_csrf)])
async def queue_hold(request: Request, entry_id: int):
    def work(tx: Session, form: Any) -> list[Message]:
        workqueue.hold_entry(tx, entry_id, "user")
        return []

    return await _entry_action(request, work)


@router.post("/queue/{entry_id:int}/release", dependencies=[Depends(verify_csrf)])
async def queue_release(request: Request, entry_id: int):
    def work(tx: Session, form: Any) -> list[Message]:
        workqueue.release_entry(tx, entry_id, "user")
        return []

    return await _entry_action(request, work)


@router.post("/queue/{entry_id:int}/prints", dependencies=[Depends(verify_csrf)])
async def queue_prints(request: Request, entry_id: int):
    def work(tx: Session, form: Any) -> list[Message]:
        prints = parse_int(form.get("prints"), field="queue.field.prints", min=1, max=workqueue.MAX_PRINTS)
        article = _article_of(tx, entry_id)
        was_paused = bool(article and article.paused)
        workqueue.set_prints(tx, entry_id, prints, "user")
        name = article.name if article else ""
        return [("queue.prints_saved", False, {"name": name, "prints": prints}), *_paused_now(article, was_paused)]

    return await _entry_action(request, work)


@router.post("/queue/{entry_id:int}/delete", dependencies=[Depends(verify_csrf)])
async def queue_delete(request: Request, entry_id: int):
    def work(tx: Session, form: Any) -> list[Message]:
        article = _article_of(tx, entry_id)
        was_paused = bool(article and article.paused)
        workqueue.delete_entry(tx, entry_id, "user")
        name = article.name if article else ""
        return [("queue.deleted", False, {"name": name}), *_paused_now(article, was_paused)]

    return await _entry_action(request, work)
