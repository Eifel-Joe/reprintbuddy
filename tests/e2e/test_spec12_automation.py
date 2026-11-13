"""The automation walked through the pages, in the order of Spec 12 (plan B4 Task 14).

The operator's steps go through the web pages (article, settings, orders, queue, article detail);
the ticks are run by hand (`run_tick`). Bambuddy is the in-memory fake: the test changes
the state of its prints (completed, deleted, switched off) the way Bambuddy would.
Steps 4 and 6 go through the page "To check" (plan B5 Task 6): the count of good pieces
and the reprint for the pieces that were not good.
"""

import asyncio
import re

from markupsafe import escape
from sqlalchemy import select

from conftest import csrf_of, form_token_of, run_tick, sm_of
from factories import make_article
from reprintbuddy import settings as st
from reprintbuddy.domain import dispatch as dispatch_domain
from reprintbuddy.domain.workqueue import OPEN_STATES
from reprintbuddy.models import Article, Dispatch, Inspection, WorkQueueEntry
from reprintbuddy.web.i18n import translate

FORM = "/articles/new/file/1/plate/1"


def en(key, **params):
    return str(escape(translate("en", key, **params)))


# ---- the hands: pages as the operator uses them ----------------------------------------


def post_form(client, page, action, **fields):
    """Submit the form of `page` whose action is `action` (CSRF and one-time token included)."""
    data = {
        "csrf_token": csrf_of(client, page),
        "form_token": form_token_of(client, page, action),
        **fields,
    }
    response = client.post(action, data=data)
    alert = re.search(r'<p class="alert" role="alert">(.*?)</p>', response.text, re.S)
    assert response.status_code == 303, (action, response.status_code, alert.group(1) if alert else None)
    return response


def set_automation(client, *, stopped=False, cap=10, insert_next=False):
    """Settings, section Automation: the switch is on; unticked boxes are not sent."""
    data = {
        "csrf_token": csrf_of(client, "/settings"),
        "automation": "on",
        "queue_cap": str(cap),
        "poll_minutes": "15",
    }
    if stopped:
        data["queue_stopped"] = "on"
    if insert_next:
        data["insert_next"] = "on"
    response = client.post("/settings/automation", data=data)
    assert response.status_code == 303, response.status_code


def create_article(client) -> int:
    """Articles > new: Template A, plate 1, with the defaults of the form (min 0, no automation)."""
    response = post_form(
        client, FORM, FORM,
        name="Template A", sku="EX-A", units_per_print="1", min_stock="0", buffer="0", target="model:A1",
        sold_alone="on",  # the box is ticked on the form (0.4.0); a browser sends it
    )
    location = response.headers["location"]
    assert location.startswith("/articles/"), location
    return int(location.split("/")[2].split("?")[0])  # p153: the filament page of the new article comes first


def edit_article(client, article_id, **fields):
    """Article > edit: minimum stock, buffer and automation."""
    path = f"/articles/{article_id}/edit"
    data = {
        "csrf_token": csrf_of(client, path),
        "name": "Template A", "sku": "EX-A", "units_per_print": "1", "target": "model:A1",
        "sold_alone": "on",  # ticked on the form since 0.4.0
        **fields,
    }
    response = client.post(path, data=data)
    assert response.status_code == 303, response.status_code


def place_order(client, article_id, reference="Order 1001", qty="1"):
    """Orders > new: one row with one article."""
    post_form(
        client, "/orders/new", "/orders/new",
        reference=reference, order_date="2026-01-15", note="", rows="5",
        item_0=f"article:{article_id}", qty_0=qty,
    )


def manual_print(client, article_id):
    post_form(client, "/queue", "/queue/manual", article_id=str(article_id), prints="1", where="end")


def entry_ids(client) -> list[int]:
    with sm_of(client)() as s:
        return list(s.scalars(select(WorkQueueEntry.id).order_by(WorkQueueEntry.position, WorkQueueEntry.id)))


def entries_of(client) -> list[tuple[int, int, str]]:
    """Reprintbuddy's work queue in order: (article, prints, origin)."""
    with sm_of(client)() as s:
        rows = s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position, WorkQueueEntry.id))
        return [(e.article_id, e.prints, e.origin) for e in rows]


# ---- the ticks and the facts behind the pages -----------------------------------------------


def tick(client, poll=False):
    run_tick(client.app.state.ticker, poll=poll)


def bambuddy_queue(fake) -> list:
    """What Bambuddy's queue holds now: pending and printing items."""
    return asyncio.run(fake.list_queue())


def article_row(client, article_id) -> Article:
    with sm_of(client)() as s:
        row = s.get(Article, article_id)
        s.expunge(row)
        return row


def numbers(client, article_id):
    """Bestand, Reserviert, Vorläufig."""
    row = article_row(client, article_id)
    return row.stock, row.reserved, row.provisional


def open_dispatches(client) -> list[tuple[int, str, int | None]]:
    with sm_of(client)() as s:
        return [
            (d.id, d.status, d.bambuddy_item_id)
            for d in s.scalars(select(Dispatch).where(Dispatch.status.in_(OPEN_STATES)).order_by(Dispatch.id))
        ]


def last_dispatch(client) -> Dispatch:
    with sm_of(client)() as s:
        row = s.scalars(select(Dispatch).order_by(Dispatch.id.desc())).first()
        s.expunge(row)
        return row


def open_count(client) -> int:
    with sm_of(client)() as s:
        return dispatch_domain.open_count(s)


def confirm_inspection(client, dispatch_id: int, scrap: int) -> None:
    """Page "To check": the operator enters the scrap of the print (default 0) and presses "Confirm"."""
    with sm_of(client)() as s:
        inspection_id = s.scalar(select(Inspection.id).where(Inspection.dispatch_id == dispatch_id))
    assert inspection_id is not None
    post_form(client, "/inspection", f"/inspection/{inspection_id}", scrap=str(scrap))


def open_inspections(client) -> int:
    with sm_of(client)() as s:
        return len(s.scalars(select(Inspection.id).where(Inspection.resolved_at.is_(None))).all())


# ---- the test -------------------------------------------------------------------------------


def test_spec12_steps(ready_client, fake_bambuddy):
    client = ready_client
    fake = fake_bambuddy
    client.app.state.bambuddy_factory = lambda: fake

    # Step 2: the article, automation on, one tick -> one print in Bambuddy, shown on the queue.
    set_automation(client)
    article_id = create_article(client)
    edit_article(client, article_id, min_stock="1", buffer="0", auto_enabled="on")
    assert article_row(client, article_id).auto_enabled is True
    assert article_row(client, article_id).min_stock == 1

    tick(client, poll=False)
    assert len(fake.enqueued) == 1
    assert len(bambuddy_queue(fake)) == 1
    first = last_dispatch(client)
    assert first.status == "queued" and first.bambuddy_item_id is not None
    page = client.get("/queue").text
    assert page.count("badge-own") == 1

    # Step 3: Bambuddy finishes the print; two regular ticks book it as provisional.
    fake.set_status(first.bambuddy_item_id, "completed")
    tick(client, poll=True)
    tick(client, poll=True)
    assert numbers(client, article_id) == (0, 0, 1)
    assert len(fake.enqueued) == 1 and len(bambuddy_queue(fake)) == 0

    # Step 4: the count of good pieces on the page "To check": stock 1, available 1;
    # the next tick adds nothing.
    assert open_inspections(client) == 1
    assert "Template A" in client.get("/inspection").text
    confirm_inspection(client, first.id, scrap=0)
    assert numbers(client, article_id) == (1, 0, 0)
    assert open_inspections(client) == 0
    done = client.get("/inspection").text
    assert en("inspections.done", name="Template A", good=1, scrap=0) in done
    assert en("inspections.replacement_created") not in done
    tick(client, poll=False)
    assert numbers(client, article_id) == (1, 0, 0)
    assert len(fake.enqueued) == 1
    assert entry_ids(client) == []

    # Step 5: queue stopped; an order takes the one piece: one entry in Reprintbuddy, nothing in Bambuddy.
    set_automation(client, stopped=True)
    place_order(client, article_id)
    assert numbers(client, article_id) == (1, 1, 0)
    tick(client, poll=False)
    entries = entry_ids(client)
    assert len(entries) == 1
    assert len(fake.enqueued) == 1 and len(bambuddy_queue(fake)) == 0

    # held back, queue running again: the tick hands nothing over.
    post_form(client, "/queue", f"/queue/{entries[0]}/hold")
    set_automation(client)
    tick(client, poll=False)
    assert len(fake.enqueued) == 1

    # released: the next tick hands it over.
    post_form(client, "/queue", f"/queue/{entries[0]}/release")
    tick(client, poll=False)
    assert len(fake.enqueued) == 2
    last_item_id = last_dispatch(client).bambuddy_item_id
    assert last_item_id is not None

    # Step 6: Bambuddy finishes that print, a regular tick books it; on the page "To check" no piece
    # is good: exactly one replacement entry at the front of Reprintbuddy's queue, handed over
    # by the next tick as a new print in Bambuddy.
    fake.set_status(last_item_id, "completed")
    tick(client, poll=True)
    assert open_inspections(client) == 1
    entries_before_scrap = entries_of(client)
    confirm_inspection(client, last_dispatch(client).id, scrap=1)
    assert open_inspections(client) == 0
    replacement_page = client.get("/inspection").text
    assert en("inspections.done", name="Template A", good=0, scrap=1) in replacement_page
    assert en("inspections.replacement_created") in replacement_page
    assert entries_of(client) == [(article_id, 1, "scrap"), *entries_before_scrap]
    assert len(entry_ids(client)) == len(entries_before_scrap) + 1
    enqueued_before = len(fake.enqueued)
    tick(client, poll=False)
    assert len(fake.enqueued) == enqueued_before + 1
    assert entries_of(client) == entries_before_scrap
    replacement_item_id = last_dispatch(client).bambuddy_item_id
    assert replacement_item_id is not None and replacement_item_id != last_item_id
    last_item_id = replacement_item_id

    # Step 7: the replacement print (still open) vanishes from Bambuddy; a regular tick pauses the
    # article, later ticks do nothing.
    fake.delete_item(last_item_id)
    tick(client, poll=True)
    row = article_row(client, article_id)
    assert row.paused is True and row.pause_reason == "missing_in_bambuddy"
    detail = client.get(f"/articles/{article_id}").text
    assert en("pause.reason.missing_in_bambuddy") in detail
    entries_before = entry_ids(client)
    tick(client, poll=True)
    tick(client, poll=False)
    assert len(fake.enqueued) == 3
    assert len(bambuddy_queue(fake)) == 0
    assert entry_ids(client) == entries_before

    # Step 8: pause lifted, cap 1, two prints by hand: exactly one print is open.
    post_form(client, f"/articles/{article_id}", f"/articles/{article_id}/resume")
    assert article_row(client, article_id).paused is False
    set_automation(client, cap=1)
    manual_print(client, article_id)
    manual_print(client, article_id)
    # Exactly the two prints by hand; no tick has run since the pause was lifted.
    assert entries_of(client) == [(article_id, 1, "manual"), (article_id, 1, "manual")]
    tick(client, poll=False)
    assert open_count(client) == 1
    assert len(fake.enqueued) == 4
    ((_open_id, _status, open_item),) = open_dispatches(client)
    fake.set_status(open_item, "completed")
    tick(client, poll=False)  # not yet polled: the cap still holds
    assert len(fake.enqueued) == 4
    tick(client, poll=True)  # the regular tick books the completion and hands the second over
    assert len(fake.enqueued) == 5
    assert open_count(client) == 1

    # Step 10: Bambuddy switched off; the warning shows, stock stays where it was.
    before = numbers(client, article_id)
    open_before = open_dispatches(client)
    assert [status for _, status, _ in open_before] == ["queued"]
    with sm_of(client)() as s:
        assert st.get_setting(s, "bambuddy_url") == "http://bambuddy.test:8000"
    fake.unavailable = True
    tick(client, poll=True)
    assert en("warning.bambuddy_unreachable") in client.get("/queue").text
    assert numbers(client, article_id) == before
    assert len(fake.enqueued) == 5
    assert article_row(client, article_id).paused is False
    assert open_dispatches(client) == open_before


def test_all_b4_get_pages_without_bambuddy(ready_client):
    client = ready_client
    client.app.state.bambuddy_factory = None  # no Bambuddy configured
    with sm_of(client).begin() as s:
        article_id = make_article(s, name="Template A").id
    for path in ("/queue", "/articles", f"/articles/{article_id}", "/settings"):
        assert client.get(path).status_code == 200, path
    automation_tab = client.get("/settings?tab=automation")
    assert automation_tab.status_code == 200
    assert 'id="automation-form"' in automation_tab.text


def test_all_b5_get_pages_without_bambuddy(ready_client):
    client = ready_client
    client.app.state.bambuddy_factory = None  # no Bambuddy configured
    for path in ("/inspection", "/assembly", "/api/inspections"):
        assert client.get(path).status_code == 200, path
