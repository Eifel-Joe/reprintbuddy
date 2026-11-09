"""Start page: the overview of Spec 10.2 (variant A).

From the top: the head line (connection to Bambuddy, last query, main switch of the automation),
the warnings, the four key figures and all articles as a table. Every part is a live region
(Spec 10.5); a region fetch renders the same template and is cut out (`web.regions`).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from reprintbuddy import settings as st
from reprintbuddy.domain import catalog, reorder, workqueue
from reprintbuddy.models import utcnow
from reprintbuddy.web import overview_view, regions
from reprintbuddy.web.deps import current_language, db_session, get_bambuddy, render
from reprintbuddy.web.engine_view import warnings_context
from reprintbuddy.web.filament_view import spools_or_none
from reprintbuddy.web.formtoken import write_transaction

router = APIRouter()


async def _suggestions(request: Request) -> int:
    """The key figure of Spec 9.4 with the function of the reorder page (p64).

    The spools are read before the write transaction, never inside one; the count writes like the
    reorder page does (new spool sorts, arrived marks closed).
    """
    spools = await spools_or_none(request)
    with write_transaction(request) as tx:
        return reorder.count_suggestions(tx, spools, utcnow())


@router.get("/")
async def overview(request: Request):
    wanted = regions.requested(request)
    # Only the key figures need the spools: a fetch of another region does not ask Bambuddy.
    suggestions = await _suggestions(request) if not wanted or "summary" in wanted else 0
    db = db_session(request)
    lang = current_language(request)
    status = request.app.state.ticker.status
    rows = catalog.list_articles(db)
    queued = sum(entry.prints for entry in workqueue.list_entries(db))
    open_prints = sum(workqueue.in_progress_prints(db).values())
    connection, connection_tone = overview_view.connection_state(status)
    return render(
        request,
        "overview.html",
        active_nav="overview",
        poll_now=get_bambuddy(request) is not None,  # the button needs a set-up Bambuddy
        connection_key=overview_view.CONNECTION_KEYS[connection],
        connection_tone=connection_tone,
        automation=bool(st.get_setting(db, "automation")),
        cards=overview_view.figures(
            lang, rows, queued_prints=queued, open_prints=open_prints, suggestions=suggestions
        ),
        rows=rows,
        hide_last_poll=True,  # the head line says it; the warnings below do not repeat it
        **warnings_context(request, db),
    )
