"""Start page: the overview of Spec 10.2, "Was ansteht" (variant B since 0.4.1).

From the top: the head line (connection to Bambuddy, last query, main switch of the automation),
the warnings and the seven cards of what is due, each leading to the page where it is done; without an
active article a hint with the way to the first one. Every part is a live region (Spec 10.5); a region
fetch renders the same template and is cut out (`web.regions`).

Nothing is counted twice: every number comes from the function of the page its card leads to.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.domain import assembled, catalog, inspection, orders, reorder, seen, workqueue
from reprintbuddy.models import utcnow
from reprintbuddy.web import overview_view, regions
from reprintbuddy.web.deps import current_language, db_session, get_bambuddy, render
from reprintbuddy.web.engine_view import warnings_context
from reprintbuddy.web.filament_view import may_write, page_spools
from reprintbuddy.web.formtoken import write_transaction

router = APIRouter()


async def _reorder_state(request: Request) -> tuple[reorder.Current, int]:
    """The suggestions and the open marks with the function of the reorder page (p64).

    The spools are read before the write transaction, never inside one; counting writes like the
    reorder page does (new spool sorts, arrived marks closed). Two exceptions, because this is the
    page everybody lands on:
    - While writes are paused (a restore or an update; the rule of the activity log,
      `app._install_activity_log`), it counts read-only from the record: after a restore the
      database may be one the running process must not write to.
    - A Bambuddy that is not set up or did not answer the last query is not asked (`page_spools`):
      the page would wait for its timeout and then count from the record anyway.
    Writes may be paused while the page waits for the spools (a restore began): it looks again after
    the wait, with no `await` until the write is done (review v0.3.15 D, finding 3).
    """
    status = request.app.state.ticker.status
    spools = None if not status.configured else await page_spools(request)
    now = utcnow()
    if not may_write(request):
        db = db_session(request)
        return reorder.current(db, None, now, write=False), len(reorder.open_marks(db, None))
    with write_transaction(request) as tx:
        state = reorder.current(tx, spools, now)
        return state, len(reorder.open_marks(tx, spools))


def _todo(db: Session, articles: list, state: reorder.Current | None, under_way: int) -> overview_view.Todo:
    open_orders = orders.list_orders(db, "open")
    checks = inspection.list_open(db)
    printing, waiting = workqueue.print_counts(db)
    below_min, overbooked = overview_view.red_counts(overview_view.status_rows(articles))
    suggestions = state.suggestions if state is not None else []
    return overview_view.Todo(
        orders_open=len(open_orders),
        orders_new=seen.new_orders(db, [o.id for o in open_orders]),
        orders_ready=sum(1 for o in open_orders if o.display_status == "ready"),
        checks=len(checks),
        check_units=sum(c.units for c in checks),
        # waiting pieces from the print, and pieces of open orders built on order; refilling the stock
        # of a variant is no assembly due (decided 11.11.2026)
        assembly_units=sum(r.awaiting for r in inspection.list_awaiting_assembly(db))
        + sum(r.qty for r in assembled.list_for_orders(db)),
        suggestions=len(suggestions),
        suggestions_new=seen.new_suggestions(db, suggestions),
        under_way=under_way,
        printing=printing,
        waiting=waiting,
        filament_short=None if state is None or state.sorts is None
        else sum(1 for s in suggestions if s.item_type == "filament"),
        below_min=below_min,
        overbooked=overbooked,
    )


@router.get("/")
async def overview(request: Request):
    wanted = regions.requested(request)
    # Only the cards need the spools: a fetch of another region does not ask Bambuddy.
    counting = not wanted or "summary" in wanted
    state, under_way = await _reorder_state(request) if counting else (None, 0)
    db = db_session(request)
    lang = current_language(request)
    status = request.app.state.ticker.status
    connection, connection_tone = overview_view.connection_state(status)
    articles = catalog.list_articles(db)
    return render(
        request,
        "overview.html",
        active_nav="overview",
        poll_now=get_bambuddy(request) is not None,  # the button needs a set-up Bambuddy
        connection_key=overview_view.CONNECTION_KEYS[connection],
        connection_tone=connection_tone,
        automation=bool(st.get_setting(db, "automation")),
        cards=overview_view.todo_cards(lang, _todo(db, articles, state, under_way)) if counting else [],
        no_articles=not articles,
        hide_last_poll=True,  # the head line says it; the warnings below do not repeat it
        **warnings_context(request, db),
    )
