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
from reprintbuddy.web.filament_view import may_write, page_spools
from reprintbuddy.web.formtoken import write_transaction

router = APIRouter()


async def _suggestions(request: Request) -> int:
    """The key figure of Spec 9.4 with the function of the reorder page (p64).

    The spools are read before the write transaction, never inside one; the count writes like the
    reorder page does (new spool sorts, arrived marks closed). Two exceptions, because this is the
    page everybody lands on:
    - While writes are paused (a restore or an update; the rule of the activity log,
      `app._install_activity_log`), it counts read-only from the record: after a restore the
      database may be one the running process must not write to.
    - A Bambuddy that is not set up or did not answer the last query is not asked: the page would
      wait for its timeout and then count from the record anyway.
    Writes may be paused while the page waits for the spools (a restore began): it looks again after
    the wait, with no `await` until the write is done (review v0.3.15 D, finding 3).
    """
    status = request.app.state.ticker.status
    spools = None if not status.configured else await page_spools(request)
    if not may_write(request):
        return reorder.count_suggestions(db_session(request), None, utcnow(), write=False)
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
    rows = overview_view.table_rows(catalog.list_articles(db))
    queued = sum(workqueue.queued_prints(db).values())  # held entries included, as in the coverage
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
