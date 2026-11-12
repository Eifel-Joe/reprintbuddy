"""Reading the state of handed-over prints and applying it (Spec 7.5-7.7, 8.1).

Plan B4 decisions 3, 6, 7, 15, 18; assumptions A2, A3, A6.
"""

import pytest
from sqlalchemy import func, select

from factories import make_article, make_dispatch, make_entry
from reprintbuddy.bambuddy.client import BambuddyRejected
from reprintbuddy.bambuddy.types import QueueItem
from reprintbuddy.domain import dispatch, tracking
from reprintbuddy.domain import workqueue as wq
from reprintbuddy.models import (
    Article,
    Dispatch,
    Inspection,
    JournalEntry,
    WorkQueueEntry,
)


# ---- helpers ---------------------------------------------------------------------------


def article(s, name="Template A", **fields):
    """Template A on file 1, plate 1 of the fake, any A1 printer."""
    fields.setdefault("target_model", "A1")
    return make_article(s, name=name, library_file_id=1, plate_id=1, **fields)


def entry(s, article_id, prints=1, **fields):
    fields.setdefault("position", s.scalar(select(func.count()).select_from(WorkQueueEntry)))
    return make_entry(s, article_id, prints, **fields)


def journal(s, kind):
    return list(
        s.scalars(select(JournalEntry).where(JournalEntry.kind == kind).order_by(JournalEntry.id))
    )


def entries_by_position(s):
    return list(s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position)))


def item(status, item_id=1):
    return QueueItem(
        id=item_id,
        status=status,
        batch_id=None,
        position=None,
        printer_id=None,
        target_model="A1",
        label="",
        created_by=None,
        library_file_id=None,
        plate_id=None,
        printer_name=None,
        manual_start=False,
        waiting_reason=None,
        started_at=None,
        completed_at=None,
        error_message=None,
    )


def open_print(db, *, units=1, **article_fields):
    """An article with a print `queued` in Bambuddy (no fake needed); returns both ids."""
    with db.begin() as s:
        a = article(s, units_per_print=units, **article_fields).id
        d = make_dispatch(s, a, status="queued", units=units, bambuddy_item_id=7).id
    return a, d


def apply(db, dispatch_id, outcome, raw):
    with db.begin() as s:
        return tracking.apply_outcome(s, dispatch_id, outcome, raw)


async def handed_over(db, fake, *, prints=1):
    """Hand `prints` prints of Template A over to the fake; returns the dispatch ids."""
    with db.begin() as s:
        a = article(s).id
        entry(s, a, prints)
    ids = []
    for _ in range(prints):
        with db.begin() as s:
            res = dispatch.reserve_next(s, cap=10)
        assert await dispatch.hand_over(fake, db, res, insert_next=False) == "queued"
        ids.append(res.dispatch_id)
    return ids


def item_id(db, dispatch_id):
    with db.begin() as s:
        return s.get(Dispatch, dispatch_id).bambuddy_item_id


def snapshot(db):
    """Everything a wrong reaction could change."""
    with db.begin() as s:
        prints = [
            (d.id, d.status, d.last_status, d.finished_at)
            for d in s.scalars(select(Dispatch).order_by(Dispatch.id))
        ]
        articles = [
            (a.id, a.stock, a.provisional, a.paused, a.pause_reason, a.consecutive_failures)
            for a in s.scalars(select(Article).order_by(Article.id))
        ]
        entries = [(e.id, e.prints, e.position) for e in entries_by_position(s)]
        journal_count = s.scalar(select(func.count()).select_from(JournalEntry))
        inspections = s.scalar(select(func.count()).select_from(Inspection))
    return prints, articles, entries, journal_count, inspections


# ---- classify --------------------------------------------------------------------------


def test_classify():
    assert tracking.classify(None) == "missing"
    for raw, outcome in tracking.STATUS_MAP.items():
        assert tracking.classify(item(raw)) == outcome
    assert tracking.STATUS_MAP == {
        "pending": "waiting",
        "printing": "printing",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "skipped": "cancelled",
    }
    assert tracking.classify(item("paused")) == "unknown"


# ---- apply_outcome ---------------------------------------------------------------------


def test_waiting_and_printing_change_state_only(db):
    a, d = open_print(db)
    assert apply(db, d, "printing", "printing") is True
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert (row.status, row.last_status, row.finished_at) == ("printing", "printing", None)
    assert apply(db, d, "waiting", "pending") is True
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "queued"
        assert s.scalar(select(func.count()).select_from(JournalEntry)) == 0
    # The same answer again changes nothing.
    assert apply(db, d, "waiting", "pending") is False


def test_completed_becomes_provisional(db):
    a, d = open_print(db, units=2, consecutive_failures=1)
    assert apply(db, d, "completed", "completed") is True
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "completed" and row.last_status == "completed"
        assert row.finished_at is not None
        art = s.get(Article, a)
        assert art.provisional == 2 and art.stock == 0
        assert art.consecutive_failures == 0
        (inspection,) = s.scalars(select(Inspection))
        assert (inspection.dispatch_id, inspection.article_id, inspection.units) == (d, a, 2)
        assert inspection.good_units is None and inspection.resolved_at is None
        (line,) = journal(s, "print_finished")
        assert line.actor == "auto" and line.qty == 2 and line.article_id == a
        assert line.details["dispatch_id"] == d


def test_completed_twice_counts_once(db):
    a, d = open_print(db, units=2)
    assert apply(db, d, "completed", "completed") is True
    assert apply(db, d, "completed", "completed") is False
    with db.begin() as s:
        assert s.get(Article, a).provisional == 2
        assert s.scalar(select(func.count()).select_from(Inspection)) == 1
        assert len(journal(s, "print_finished")) == 1


def test_failed_replacement_at_start(db):
    a, d = open_print(db, min_stock=3)  # p411: a minimum stock, so the failure leaves the target short
    with db.begin() as s:
        b = make_article(s, name="Template B", library_file_id=2, plate_id=1).id
        first = entry(s, b).id
        second = entry(s, a).id
    assert apply(db, d, "failed", "failed") is True
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert (row.status, row.last_status) == ("failed", "failed")
        assert row.finished_at is not None
        art = s.get(Article, a)
        assert art.consecutive_failures == 1 and not art.paused
        assert art.provisional == 0
        queue = entries_by_position(s)
        assert [e.id for e in queue[1:]] == [first, second]
        new = queue[0]
        assert (new.article_id, new.prints, new.origin) == (a, 1, "failure")
        (line,) = journal(s, "print_failed")
        assert line.actor == "auto" and line.article_id == a
        assert line.details["dispatch_id"] == d
        (created,) = journal(s, "queue_entry_created")
        assert created.actor == "auto" and created.details["dispatch_id"] == d
        assert journal(s, "automation_paused") == []


def test_two_failures_pause_and_still_replace(db):
    a, d1 = open_print(db, min_stock=3)  # p411: a minimum stock, so both failures leave the target short
    with db.begin() as s:
        d2 = make_dispatch(s, a, status="printing", bambuddy_item_id=8).id
    apply(db, d1, "failed", "failed")
    assert apply(db, d2, "failed", "failed") is True
    with db.begin() as s:
        art = s.get(Article, a)
        assert art.consecutive_failures == 2
        assert art.paused and art.pause_reason == "two_failures"
        failures = [e for e in entries_by_position(s) if e.origin == "failure"]
        assert sum(e.prints for e in failures) == 2
        (paused,) = journal(s, "automation_paused")
        assert paused.actor == "auto" and paused.details["reason"] == "two_failures"


def test_success_resets_failures(db):
    a, d1 = open_print(db)
    with db.begin() as s:
        d2 = make_dispatch(s, a, status="queued", bambuddy_item_id=8).id
        d3 = make_dispatch(s, a, status="queued", bambuddy_item_id=9).id
    apply(db, d1, "failed", "failed")
    apply(db, d2, "completed", "completed")
    apply(db, d3, "failed", "failed")
    with db.begin() as s:
        art = s.get(Article, a)
        assert art.consecutive_failures == 1 and not art.paused


@pytest.mark.parametrize("raw", ["cancelled", "skipped"])
def test_cancelled_pauses(db, raw):
    a, d = open_print(db, units=3)
    with db.begin() as s:
        assert wq.in_progress_units(s) == {a: 3}
    assert apply(db, d, tracking.classify(item(raw)), raw) is True
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert (row.status, row.last_status) == ("cancelled", raw)
        assert row.finished_at is not None
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "cancelled_in_bambuddy"
        assert art.provisional == 0 and art.consecutive_failures == 0
        assert entries_by_position(s) == []
        assert wq.in_progress_units(s) == {}
        (line,) = journal(s, "print_cancelled")
        assert line.actor == "auto" and line.qty == 3
        assert line.details["dispatch_id"] == d and line.details["status"] == raw


def test_missing_pauses(db):
    a, d = open_print(db)
    with db.begin() as s:
        s.get(Dispatch, d).last_status = "printing"
    assert apply(db, d, tracking.classify(None), None) is True
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "missing" and row.finished_at is not None
        # No answer is not a state: the last one Bambuddy gave stays visible.
        assert row.last_status == "printing"
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "missing_in_bambuddy"
        assert entries_by_position(s) == []
        (line,) = journal(s, "print_missing")
        assert line.actor == "auto" and line.details["dispatch_id"] == d


def test_unknown_status_changes_nothing(db):
    a, d = open_print(db)
    before = snapshot(db)
    assert apply(db, d, tracking.classify(item("paused")), "paused") is False
    assert snapshot(db) == before


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "missing", "returned"])
def test_finished_print_is_left_alone(db, status):
    with db.begin() as s:
        a = article(s).id
        d = make_dispatch(s, a, status=status, bambuddy_item_id=7).id
    before = snapshot(db)
    for outcome in ("waiting", "printing", "completed", "failed", "cancelled", "missing"):
        assert apply(db, d, outcome, "x") is False
    assert snapshot(db) == before


# ---- confirm_dispatch uses classify ----------------------------------------------------


@pytest.mark.parametrize(("raw", "state"), [("pending", "queued"), ("printing", "printing")])
def test_confirm_dispatch_state_by_classify(db, raw, state):
    with db.begin() as s:
        a = article(s).id
        d = make_dispatch(s, a, status="unclear").id
        dispatch.confirm_dispatch(s, d, item(raw, 11))
        row = s.get(Dispatch, d)
        assert (row.status, row.last_status, row.bambuddy_item_id) == (state, raw, 11)


def test_confirm_dispatch_already_completed(db):
    with db.begin() as s:
        a = article(s).id
        d = make_dispatch(s, a, status="unclear").id
        dispatch.confirm_dispatch(s, d, item("completed", 11))
        assert s.get(Dispatch, d).status == "completed"
        assert s.get(Article, a).provisional == 1
        assert [j.kind for j in s.scalars(select(JournalEntry).order_by(JournalEntry.id))] == [
            "print_dispatched",
            "print_finished",
        ]


def test_confirm_dispatch_unknown_stays_queued(db):
    with db.begin() as s:
        a = article(s).id
        d = make_dispatch(s, a, status="unclear").id
        dispatch.confirm_dispatch(s, d, item("paused", 11))
        row = s.get(Dispatch, d)
        assert (row.status, row.last_status) == ("queued", "paused")
        assert s.get(Article, a).provisional == 0


# ---- polling ---------------------------------------------------------------------------


async def test_poll_dispatches(db, fake_bambuddy):
    d1, d2 = await handed_over(db, fake_bambuddy, prints=2)
    fake_bambuddy.set_status(item_id(db, d1), "completed")
    fake_bambuddy.set_status(item_id(db, d2), "printing")
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert report == tracking.PollReport(
        reachable=True, changed=2, unexpected=[], waiting_review=[], error=None
    )
    with db.begin() as s:
        assert s.get(Dispatch, d1).status == "completed"
        assert s.get(Dispatch, d2).status == "printing"
        assert s.get(Article, s.get(Dispatch, d1).article_id).provisional == 1
    # Nothing new in Bambuddy: nothing changes, the finished print is not asked again.
    fake_bambuddy.calls.clear()
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert report.changed == 0
    assert fake_bambuddy.calls == ["get_queue_item"]


async def test_poll_oldest_first(db, fake_bambuddy):
    d1, d2, d3 = await handed_over(db, fake_bambuddy, prints=3)
    asked = []
    original = fake_bambuddy.get_queue_item

    async def spy(item_id):
        asked.append(item_id)
        return await original(item_id)

    fake_bambuddy.get_queue_item = spy
    await tracking.poll_dispatches(fake_bambuddy, db)
    assert asked == [item_id(db, d) for d in (d1, d2, d3)]


async def test_poll_missing_item(db, fake_bambuddy):
    (d,) = await handed_over(db, fake_bambuddy)
    fake_bambuddy.delete_item(item_id(db, d))
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert report.changed == 1 and report.unexpected == []
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "missing"
        assert s.get(Article, row.article_id).pause_reason == "missing_in_bambuddy"


async def test_manual_start_reported(db, fake_bambuddy):
    (d,) = await handed_over(db, fake_bambuddy)
    own = item_id(db, d)
    fake_bambuddy.set_manual_start(own, True)
    foreign = fake_bambuddy.add_foreign_item()
    fake_bambuddy.set_manual_start(foreign.id, True)
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert report.waiting_review == [own]
    assert report.changed == 0
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "queued"


async def test_poll_unexpected_collected(db, fake_bambuddy):
    d1, d2 = await handed_over(db, fake_bambuddy, prints=2)
    fake_bambuddy.set_status(item_id(db, d1), "paused")
    fake_bambuddy.set_status(item_id(db, d2), "paused")
    before = snapshot(db)
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert report.unexpected == ["paused"]
    assert report.changed == 0 and report.reachable and report.error is None
    assert snapshot(db) == before


async def test_poll_unavailable_changes_nothing(db, fake_bambuddy):
    (d,) = await handed_over(db, fake_bambuddy)
    fake_bambuddy.set_status(item_id(db, d), "completed")
    fake_bambuddy.unavailable = True
    before = snapshot(db)
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert report.reachable is False and report.changed == 0 and report.error is None
    assert snapshot(db) == before


async def test_poll_unexpected_answer_counts_as_unavailable(db, fake_bambuddy):
    from reprintbuddy.bambuddy.client import BambuddyUnexpected

    (d,) = await handed_over(db, fake_bambuddy)
    fake_bambuddy.set_status(item_id(db, d), "completed")
    fake_bambuddy.fail_next("get_queue_item", BambuddyUnexpected("no JSON"))
    before = snapshot(db)
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert report.reachable is False
    assert snapshot(db) == before


async def test_poll_rejected_changes_nothing(db, fake_bambuddy):
    (d,) = await handed_over(db, fake_bambuddy)
    fake_bambuddy.set_status(item_id(db, d), "completed")
    fake_bambuddy.fail_next("get_queue_item", BambuddyRejected("Forbidden", status=403))
    before = snapshot(db)
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert report.error == "403: Forbidden"
    assert report.reachable is True and report.changed == 0
    assert snapshot(db) == before


async def test_poll_ignores_foreign_items(db, fake_bambuddy):
    (d,) = await handed_over(db, fake_bambuddy)
    foreign = fake_bambuddy.add_foreign_item(status="printing")
    asked = []
    original = fake_bambuddy.get_queue_item

    async def spy(item_id):
        asked.append(item_id)
        return await original(item_id)

    fake_bambuddy.get_queue_item = spy
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert asked == [item_id(db, d)]
    assert foreign.id not in asked
    assert report.changed == 0


async def test_poll_skips_unsettled_prints(db, fake_bambuddy):
    """`sending`/`unclear` prints belong to resolve_pending (Task 8), not to the poll."""
    with db.begin() as s:
        a = article(s).id
        make_dispatch(s, a, status="unclear", bambuddy_item_id=5)
        make_dispatch(s, a, status="sending", bambuddy_item_id=6)
    report = await tracking.poll_dispatches(fake_bambuddy, db)
    assert "get_queue_item" not in fake_bambuddy.calls
    assert report.reachable and report.changed == 0


# ---- p411: a replacement only up to the target (Spec 7.5, 0.4.5) ------------------------


def failed_with(db, *, origin="rule", **article_fields):
    """One print of the article fails; returns the article id and the queue afterwards."""
    with db.begin() as s:
        a = article(s, **article_fields).id
        d = make_dispatch(s, a, status="printing", units=1, origin=origin, bambuddy_item_id=7).id
    apply(db, d, "failed", "failed")
    with db.begin() as s:
        return a, [(e.prints, e.origin) for e in entries_by_position(s)]


def test_p411_a_print_over_the_target_gets_no_replacement(db):
    """After a cancel a running print stayed although it was over the target (Spec 7.2a), and failed:
    stock 7 >= target 6 (minimum 4 + buffer 2), so nothing is missing and nothing is printed again."""
    a, queue = failed_with(db, stock=7, min_stock=4, buffer=2)
    assert queue == []
    with db.begin() as s:
        (line,) = journal(s, "replacement_not_needed")
        assert line.actor == "auto" and line.article_id == a and line.qty == 1
        assert line.details["coverage"] == 7 and line.details["target"] == 6
        assert journal(s, "print_failed")  # the failure itself is in the journal as before


def test_p411_a_print_still_needed_is_replaced(db):
    _, queue = failed_with(db, stock=5, min_stock=4, buffer=2)
    assert queue == [(1, "failure")]


def test_p411_the_replacement_is_cut_to_what_is_missing(db):
    """Scrap of 3 prints (2 units each) but only one unit is missing to the target: one print."""
    from reprintbuddy.domain import inspection

    with db.begin() as s:
        a = article(s, units_per_print=2, stock=5, min_stock=4, buffer=2).id
        d = make_dispatch(s, a, status="completed", units=6, origin="rule").id
        insp = Inspection(article_id=a, dispatch_id=d, units=6)
        s.add(insp)
        art = s.get(Article, a)
        art.provisional = 6
        s.flush()
        insp_id = insp.id
    with db.begin() as s:
        result = inspection.resolve_scrap(s, insp_id, 6, "user")
    with db.begin() as s:
        assert [(e.prints, e.origin) for e in entries_by_position(s)] == [(1, "scrap")]
        assert result.replacement_entry_id is not None


@pytest.mark.parametrize("origin", ["manual", "failure", "scrap"])
def test_p411_prints_by_hand_and_replacements_are_always_replaced(db, origin):
    _, queue = failed_with(db, origin=origin, stock=9, min_stock=4, buffer=2)
    assert queue == [(1, "failure")]


def test_p411_a_part_counts_its_demand_from_assembly(db):
    """A part of a stock variant: target = minimum + buffer + demand from assembly (Spec 7.1, 7.5)."""
    from factories import make_assembled

    with db.begin() as s:
        clip = article(s, name="Clip", stock=3, min_stock=0, buffer=0).id
        # box: minimum 1, available 0 -> missing 1 box -> demand 2 clips; clip coverage 3 >= 2
        make_assembled(s, name="Storage box", sku="EX-B1", base=[(clip, 2)], min_stock=1, stock=0)
        d = make_dispatch(s, clip, status="printing", units=1, origin="rule", bambuddy_item_id=7).id
    apply(db, d, "failed", "failed")
    with db.begin() as s:
        assert entries_by_position(s) == []
        (line,) = journal(s, "replacement_not_needed")
        assert (line.details["coverage"], line.details["target"]) == (3, 2)
    with db.begin() as s:
        s.get(Article, clip).stock = 1  # now a clip is missing to the demand
        d2 = make_dispatch(s, clip, status="printing", units=1, origin="rule", bambuddy_item_id=8).id
    apply(db, d2, "failed", "failed")
    with db.begin() as s:
        assert [(e.prints, e.origin) for e in entries_by_position(s)] == [(1, "failure")]
