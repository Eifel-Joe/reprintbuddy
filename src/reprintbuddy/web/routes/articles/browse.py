"""The article list and its quick change of the print target (Spec 5.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy.domain import catalog
from reprintbuddy.domain.status import STATES, summarize
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Article
from reprintbuddy.web import kpis, listing, regions
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_or_404, current_language, get_db, local_next, render
from reprintbuddy.web.forms import run_form_action, parse_int
from reprintbuddy.web.i18n import translate

router = APIRouter()


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
    every = catalog.list_articles(db, archived=archived)
    lang = current_language(request)
    t = lambda key: translate(lang, key)  # noqa: E731
    # "enough" and "paused" get no card: the cards name what needs attention
    icon, label, named = ("archive", "kpi.articles_archived", []) if archived else (
        "package", "kpi.articles",
        summarize((row.status for row in every), ("overbooked", "below_min", "low", "not_deliverable")),  # p363
    )
    page = listing.list_page(
        request, page="articles", rows=every, states=STATES["article"], status_of=lambda row: row.status,
        # the search also finds a variant by its number (0.4.0, p341)
        search_texts=lambda row: (row.name, row.sku, *(h.sku for h in (row.assembled.holders if row.assembled else ()))),
        q=q, status_filter=not archived, view="archived" if archived else None,
        cards=kpis.state_cards(t, "article", icon, len(every), t(label), named),
    )
    shown = page.shown
    # A refused target quantity (`article_target`) is shown once, at the field of its article. Like a
    # message it is not used up by a fetch of regions (another tab on the same cookie): it belongs to
    # the page that follows the redirect. When the article has no field here (archived on another device
    # meanwhile, hidden by the search or the filter) the text stands above the list instead.
    target_error = None if regions.requested(request) else request.session.pop(TARGET_ERROR_KEY, None)
    orphan_error = None
    if target_error is not None and not any(row.id == target_error["id"] for row in shown):
        target_error, orphan_error = None, target_error["text"]
    return render(
        request, "articles_list.html", active_nav="articles", archived=archived, target_error=target_error,
        orphan_error=orphan_error, rows=shown, status=page.status, **page.context,
        here=request.url.path + (f"?{request.url.query}" if request.url.query else ""),  # the switches return here
    )


TARGET_ERROR_KEY = "target_error"  # session: {"id": article id, "value": what was typed, "text": the refusal}


@router.post("/articles/{article_id:int}/target", dependencies=[Depends(verify_csrf)])
async def article_target(request: Request, article_id: int, db: Session = Depends(get_db)):
    """A new target quantity from the list or a tile: the minimum stock becomes target - buffer
    (`catalog.set_target_quantity`, the same checks as the edit form).

    Back to the list the form came from (`next`, a local path). A refusal comes back there too and
    shows at the field of the article (the list reads it from the session once), with what was typed.
    Only this form saves: the arrows of a number field (spinbox.js) change the value in the field and
    never send it, because every saved change lets rule 7.1 plan prints, and three clicks on an arrow
    must not be three changes.
    """
    name = get_or_404(db, Article, article_id).name
    form = await request.form()
    after = local_next(form) or "/articles"
    raw = str(form.get("target", ""))

    def work(tx):
        target = parse_int(raw, field="articles.target_quantity", min=0, max=MAX_QTY)
        catalog.set_target_quantity(tx, article_id, target)
        return {"name": name, "target": target}

    def on_error(text: str, status: int, exc: Exception):
        request.session[TARGET_ERROR_KEY] = {"id": article_id, "value": raw[:24], "text": text}
        return RedirectResponse(after, status_code=303)

    return await run_form_action(
        request, work=work, success_url=after, flash_key="articles.target_saved", on_error=on_error
    )
