"""Settling unclear hand-overs without ever printing twice (Spec 7.6, 7.7, plan B4 decision 5).

Review focus 1 and 3: a print whose `enqueue` was sent is searched in Bambuddy by its
batch and adopted; when nothing is found the article pauses and nothing is sent again.
Only a print that was certainly never sent goes back to the work queue.
"""

import dataclasses

import pytest
from sqlalchemy import func, select

from factories import make_article, make_dispatch, make_entry
from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import EnqueueRequest
from reprintbuddy.domain import dispatch
from reprintbuddy.domain import workqueue as wq
from reprintbuddy.domain.stock import write_journal
from reprintbuddy.models import (
    Article,
    Dispatch,
    Inspection,
    JournalEntry,
    WorkQueueEntry,
    utcnow,
)


# ---- helpers ---------------------------------------------------------------------------


def article(s, name="Template A", *, plate=1, **fields):
    """Template A on file 1 of the fake, plate 1 unless told otherwise, any A1 printer."""
    fields.setdefault("target_model", "A1")
    return make_article(s, name=name, library_file_id=1, plate_id=plate, **fields)


def entry(s, article_id, prints=1, **fields):
    fields.setdefault("position", s.scalar(select(func.count()).select_from(WorkQueueEntry)))
    return make_entry(s, article_id, prints, **fields)


def journal(s, kind):
    return list(
        s.scalars(select(JournalEntry).where(JournalEntry.kind == kind).order_by(JournalEntry.id))
    )


def queue_of(s):
    return [(e.article_id, e.prints, e.origin) for e in wq.list_entries(s)]


async def handed_over(db, fake, *, prints=1, plate=1):
    """An article with `prints` prints in the work queue; hands the first one over."""
    with db.begin() as s:
        a = article(s, f"Template {plate}", plate=plate).id
        entry(s, a, prints)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    result = await dispatch.hand_over(fake, db, res, insert_next=False)
    return a, res.dispatch_id, result


async def lost_answer(db, fake, *, prints=1, effect=True, plate=1):
    """`enqueue` sent, the answer lost: with `effect` Bambuddy took the print anyway."""
    fake.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=effect)
    a, d, result = await handed_over(db, fake, prints=prints, plate=plate)
    assert result == "unclear"
    return a, d


async def crashed_after_mark(db, fake, a):
    """Reserved, batch made, `enqueue_sent_at` committed - then the process died."""
    with db.begin() as s:
        entry(s, a)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    batch_id = await fake.ensure_batch(res.batch_name, res.external_ref, res.library_file_id)
    with db.begin() as s:
        s.get(Dispatch, res.dispatch_id).bambuddy_batch_id = batch_id
        dispatch.mark_sent(s, res.dispatch_id)
    return res.dispatch_id, batch_id


def in_batch(fake, batch_id, status="pending"):
    """A Bambuddy entry in `batch_id`, without going through `enqueue`."""
    return dataclasses.replace(fake.add_foreign_item(status=status), batch_id=batch_id)


def batch_of(db, dispatch_id):
    with db.begin() as s:
        return s.get(Dispatch, dispatch_id).bambuddy_batch_id


# ---- found: adopt, never send again ----------------------------------------------------


async def test_lost_response_found_not_resent(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    (found,) = await fake_bambuddy.find_items_by_batch(batch_of(db, d))
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report == dispatch.ResolveReport(
        reachable=True, resolved=1, unresolved=0, duplicates=[], lost=[]
    )
    assert len(fake_bambuddy.enqueued) == 1
    assert fake_bambuddy.calls == ["find_items_by_batch"]
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "queued"
        assert row.bambuddy_item_id == found.id
        assert row.last_status == "pending"
        assert row.dispatched_at is not None and row.finished_at is None
        (line,) = journal(s, "print_dispatched")
        assert line.actor == "auto" and line.article_id == a and line.qty == 1
        assert line.details == {
            "dispatch_id": d,
            "item_id": found.id,
            "batch_id": row.bambuddy_batch_id,
        }
        assert not s.get(Article, a).paused
        assert dispatch.open_count(s) == 1
    # Settled: a second run has nothing left to search.
    fake_bambuddy.calls.clear()
    again = await dispatch.resolve_pending(fake_bambuddy, db)
    assert again == dispatch.ResolveReport(True, 0, 0, [], [])
    assert fake_bambuddy.calls == []


async def test_resolved_item_already_finished(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    (found,) = await fake_bambuddy.find_items_by_batch(batch_of(db, d))
    fake_bambuddy.set_status(found.id, "completed")

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1 and report.lost == []
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "completed" and row.last_status == "completed"
        assert row.bambuddy_item_id == found.id
        assert row.finished_at is not None
        art = s.get(Article, a)
        assert art.provisional == 1 and art.stock == 0
        (inspection,) = s.scalars(select(Inspection))
        assert inspection.dispatch_id == d
        assert len(journal(s, "print_dispatched")) == 1
    assert len(fake_bambuddy.enqueued) == 1


async def test_resolved_item_unknown_state_waits_for_poll(db, fake_bambuddy):
    """Bambuddy names a state `classify` does not know: adopted, the poll reports it."""
    a, d = await lost_answer(db, fake_bambuddy)
    (found,) = await fake_bambuddy.find_items_by_batch(batch_of(db, d))
    fake_bambuddy.set_status(found.id, "paused")

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "queued" and row.last_status == "paused"
        assert row.bambuddy_item_id == found.id and row.finished_at is None


async def test_duplicates_adopt_first_and_pause(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    batch_id = batch_of(db, d)
    (first,) = await fake_bambuddy.find_items_by_batch(batch_id)
    second = await fake_bambuddy.enqueue(EnqueueRequest(1, 1, "A1", None, batch_id, False, None))
    assert first.id < second.id
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.duplicates == [[first.id, second.id]]
    assert report.resolved == 1 and report.lost == [] and report.unresolved == 0
    assert "enqueue" not in fake_bambuddy.calls
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "queued" and row.bambuddy_item_id == first.id
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "unclear"
        (paused,) = journal(s, "automation_paused")
        assert paused.actor == "auto" and paused.article_id == a
        assert paused.details["item_ids"] == [first.id, second.id]
        assert paused.details["reason"] == "unclear"
        assert len(journal(s, "print_dispatched")) == 1
        (dup,) = journal(s, "print_duplicates")
        assert dup.actor == "auto" and dup.article_id == a
        assert dup.details == {
            "dispatch_id": d,
            "batch_id": batch_id,
            "item_ids": [first.id, second.id],
        }


async def test_duplicates_journaled_when_article_already_paused(db, fake_bambuddy):
    """Final review 2: the ids are written down even when the pause writes nothing."""
    from reprintbuddy.domain.pauses import pause_article

    a, d = await lost_answer(db, fake_bambuddy)
    batch_id = batch_of(db, d)
    (first,) = await fake_bambuddy.find_items_by_batch(batch_id)
    second = await fake_bambuddy.enqueue(EnqueueRequest(1, 1, "A1", None, batch_id, False, None))
    with db.begin() as s:
        pause_article(s, a, "two_failures", "auto")

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.duplicates == [[first.id, second.id]]
    with db.begin() as s:
        assert s.get(Article, a).pause_reason == "two_failures"
        assert len(journal(s, "automation_paused")) == 1  # only the earlier pause
        (dup,) = journal(s, "print_duplicates")
        assert dup.article_id == a
        assert dup.details["item_ids"] == [first.id, second.id]
        assert dup.details["dispatch_id"] == d and dup.details["batch_id"] == batch_id


def test_adopt_writes_dispatched_once(db, fake_bambuddy):
    """A print adopted after an earlier journal line keeps that single line."""
    with db.begin() as s:
        a = article(s).id
        row = make_dispatch(
            s, a, status="unclear", bambuddy_batch_id=3, enqueue_sent_at=utcnow()
        )
        write_journal(
            s, kind="print_dispatched", actor="auto", qty=1, article_id=a,
            details={"dispatch_id": row.id, "item_id": 9, "batch_id": 3},
        )
        other = make_dispatch(s, a, status="queued", bambuddy_item_id=8)
        write_journal(
            s, kind="print_dispatched", actor="auto", qty=1, article_id=a,
            details={"dispatch_id": other.id, "item_id": 8, "batch_id": 2},
        )
        item = in_batch(fake_bambuddy, 3, "printing")
        dispatch.adopt(s, row.id, item)
        assert row.status == "printing" and row.bambuddy_item_id == item.id
        assert len(journal(s, "print_dispatched")) == 2


def test_adopt_refuses_settled_print(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s).id
        row = make_dispatch(s, a, status="missing", enqueue_sent_at=utcnow())
        with pytest.raises(ValueError):
            dispatch.adopt(s, row.id, in_batch(fake_bambuddy, 3))
        assert row.status == "missing" and row.bambuddy_item_id is None


# ---- sent but not found: pause, never send again ---------------------------------------


async def test_sent_but_not_found_pauses(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy, prints=2, effect=False)
    batch_id = batch_of(db, d)
    with db.begin() as s:
        before = queue_of(s)
    assert before == [(a, 1, "rule")]
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report == dispatch.ResolveReport(
        reachable=True, resolved=0, unresolved=0, duplicates=[], lost=[d]
    )
    assert fake_bambuddy.calls == ["find_items_by_batch"]
    assert fake_bambuddy.enqueued == []
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "missing"
        assert row.finished_at is not None
        assert row.bambuddy_item_id is None
        (line,) = journal(s, "print_missing")
        assert line.actor == "auto" and line.article_id == a and line.qty == 1
        assert line.details["reason"] == "not_found_after_send"
        assert line.details["batch_id"] == batch_id
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "unclear"
        (paused,) = journal(s, "automation_paused")
        assert paused.details["reason"] == "unclear" and paused.actor == "auto"
        assert queue_of(s) == before
        assert journal(s, "print_returned") == []
        assert dispatch.open_count(s) == 0
    # Settled: nothing is searched or sent on the next run.
    fake_bambuddy.calls.clear()
    assert await dispatch.resolve_pending(fake_bambuddy, db) == dispatch.ResolveReport(
        True, 0, 0, [], []
    )
    assert fake_bambuddy.calls == []


async def test_empty_search_does_not_resend(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    (found,) = await fake_bambuddy.find_items_by_batch(batch_of(db, d))
    fake_bambuddy.delete_item(found.id)  # like "clear history" in Bambuddy

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.lost == [d] and report.resolved == 0
    assert len(fake_bambuddy.enqueued) == 1
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "missing"
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "unclear"
        assert queue_of(s) == []


async def test_crash_after_mark_before_send_pauses(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s).id
    d, batch_id = await crashed_after_mark(db, fake_bambuddy, a)
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "sending"
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.lost == [d] and report.unresolved == 0
    assert fake_bambuddy.calls == ["find_items_by_batch"]
    assert fake_bambuddy.enqueued == []
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "missing"
        (line,) = journal(s, "print_missing")
        assert line.details["batch_id"] == batch_id
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "unclear"
        assert queue_of(s) == []


async def test_sent_without_batch_lost_without_search(db, fake_bambuddy):
    """The mark comes after the batch, so this cannot happen - if it does, pause."""
    with db.begin() as s:
        a = article(s).id
        d = make_dispatch(s, a, status="sending", enqueue_sent_at=utcnow()).id

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.lost == [d]
    assert fake_bambuddy.calls == []
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "missing"
        (line,) = journal(s, "print_missing")
        assert line.details["batch_id"] is None
        assert s.get(Article, a).pause_reason == "unclear"


def test_mark_lost_refuses_unsent_or_settled(db):
    with db.begin() as s:
        a = article(s).id
        unsent = make_dispatch(s, a, status="sending")
        settled = make_dispatch(s, a, status="queued", enqueue_sent_at=utcnow())
        for row in (unsent, settled):
            with pytest.raises(ValueError):
                dispatch.mark_lost(s, row.id)
        assert (unsent.status, settled.status) == ("sending", "queued")
        assert journal(s, "print_missing") == []


# ---- never sent: back to the work queue, no network ------------------------------------


async def test_never_sent_returns_to_queue(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s).id
        b = article(s, "Template B", plate=2).id
        entry(s, a)
        entry(s, b)
    with db.begin() as s:
        bare = dispatch.reserve_next(s, cap=10)  # then cancelled before ensure_batch
    with db.begin() as s:
        with_batch = dispatch.reserve_next(s, cap=10)
    batch_id = await fake_bambuddy.ensure_batch(
        with_batch.batch_name, with_batch.external_ref, with_batch.library_file_id
    )
    with db.begin() as s:
        s.get(Dispatch, with_batch.dispatch_id).bambuddy_batch_id = batch_id
        assert queue_of(s) == []
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert fake_bambuddy.calls == []
    assert report == dispatch.ResolveReport(True, 0, 0, [], [])
    with db.begin() as s:
        for res in (bare, with_batch):
            row = s.get(Dispatch, res.dispatch_id)
            assert row.status == "returned" and row.message == "not_sent"
        # Put back newest first, so both land where they were.
        assert queue_of(s) == [(a, 1, "rule"), (b, 1, "rule")]
        lines = journal(s, "print_returned")
        assert [line.details["reason"] for line in lines] == ["not_sent", "not_sent"]
        assert not s.get(Article, a).paused and not s.get(Article, b).paused
        assert dispatch.open_count(s) == 0


async def test_never_sent_returned_while_unreachable(db, fake_bambuddy):
    """Putting back needs no network, so it happens even when Bambuddy is down."""
    with db.begin() as s:
        a = article(s).id
        entry(s, a)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    fake_bambuddy.unavailable = True

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert fake_bambuddy.calls == []
    assert report.reachable is True and report.unresolved == 0
    with db.begin() as s:
        assert s.get(Dispatch, res.dispatch_id).status == "returned"
        assert queue_of(s) == [(a, 1, "rule")]


# ---- Bambuddy unreachable or refusing: stay unclear ------------------------------------


async def test_resolve_unavailable_keeps_unclear(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    d2, _ = await crashed_after_mark(db, fake_bambuddy, a)
    fake_bambuddy.calls.clear()
    fake_bambuddy.unavailable = True

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report == dispatch.ResolveReport(
        reachable=False, resolved=0, unresolved=2, duplicates=[], lost=[]
    )
    # Stops at the first failed search; nothing is sent.
    assert fake_bambuddy.calls == ["find_items_by_batch"]
    assert len(fake_bambuddy.enqueued) == 1
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "unclear"
        assert s.get(Dispatch, d2).status == "unclear"  # was `sending` with the mark
        assert s.get(Dispatch, d).bambuddy_item_id is None
        assert len(journal(s, "print_dispatch_unclear")) == 2
        assert journal(s, "print_missing") == []
        assert not s.get(Article, a).paused
        assert queue_of(s) == []
    # Reachable again: both are settled, still nothing sent.
    fake_bambuddy.unavailable = False
    later = await dispatch.resolve_pending(fake_bambuddy, db)
    assert later.resolved == 1 and later.lost == [d2] and later.unresolved == 0
    assert len(fake_bambuddy.enqueued) == 1


async def test_resolve_rejected_keeps_unclear(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    d2, _ = await crashed_after_mark(db, fake_bambuddy, a)
    fake_bambuddy.calls.clear()
    fake_bambuddy.fail_next("find_items_by_batch", BambuddyRejected("bad", status=422))

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report == dispatch.ResolveReport(
        reachable=True, resolved=0, unresolved=2, duplicates=[], lost=[]
    )
    assert fake_bambuddy.calls == ["find_items_by_batch"]
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "unclear"
        assert s.get(Dispatch, d2).status == "unclear"
        assert journal(s, "print_missing") == []
        assert not s.get(Article, a).paused


async def test_resolve_keeps_what_was_settled_before_a_failure(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    d2, _ = await crashed_after_mark(db, fake_bambuddy, a)
    original = fake_bambuddy.find_items_by_batch
    seen = []

    async def second_fails(batch_id):
        seen.append(batch_id)
        if len(seen) == 2:
            raise BambuddyUnavailable("timeout")
        return await original(batch_id)

    fake_bambuddy.find_items_by_batch = second_fails

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert seen == [batch_of(db, d), batch_of(db, d2)]  # oldest first
    assert report == dispatch.ResolveReport(False, 1, 1, [], [])
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "queued"
        assert s.get(Dispatch, d2).status == "unclear"


# ---- resolve_pending never enqueues ----------------------------------------------------


async def test_resolve_never_enqueues(db, fake_bambuddy):
    """Every case at once: resolve only searches, the work queue keeps its prints."""
    found_a, found_d = await lost_answer(db, fake_bambuddy, plate=1)  # found, waiting
    _, gone_d = await lost_answer(db, fake_bambuddy, effect=False, plate=2)  # sent, not found
    _, done_d = await lost_answer(db, fake_bambuddy, plate=3)  # found, completed
    (done,) = await fake_bambuddy.find_items_by_batch(batch_of(db, done_d))
    fake_bambuddy.set_status(done.id, "completed")
    _, dup_d = await lost_answer(db, fake_bambuddy, plate=4)  # found twice
    dup_batch = batch_of(db, dup_d)
    await fake_bambuddy.enqueue(EnqueueRequest(1, 4, "A1", None, dup_batch, False, None))
    with db.begin() as s:
        crash_a = article(s, "Template 5", plate=5).id
    crash_d, _ = await crashed_after_mark(db, fake_bambuddy, crash_a)  # marked, never sent
    with db.begin() as s:
        never_a = article(s, "Template 6", plate=6).id
        waiting_a = article(s, "Template 7", plate=7).id
        entry(s, never_a)
        entry(s, waiting_a, 2, held=True)
    with db.begin() as s:
        never = dispatch.reserve_next(s, cap=20)  # reserved, never sent
        assert never.article_id == never_a
        queue_before = queue_of(s)
    assert queue_before == [(waiting_a, 2, "rule")]
    enqueued_before = len(fake_bambuddy.enqueued)
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert "enqueue" not in fake_bambuddy.calls
    assert "ensure_batch" not in fake_bambuddy.calls
    assert set(fake_bambuddy.calls) == {"find_items_by_batch"}
    assert len(fake_bambuddy.enqueued) == enqueued_before
    assert report.reachable is True and report.unresolved == 0
    assert report.resolved == 3
    assert sorted(report.lost) == sorted([gone_d, crash_d])
    assert len(report.duplicates) == 1
    with db.begin() as s:
        states = {
            d: s.get(Dispatch, d).status
            for d in (found_d, gone_d, done_d, dup_d, crash_d, never.dispatch_id)
        }
        assert states == {
            found_d: "queued",
            gone_d: "missing",
            done_d: "completed",
            dup_d: "queued",
            crash_d: "missing",
            never.dispatch_id: "returned",
        }
        # Only the never-sent print went back; nothing else was added.
        assert queue_of(s) == [(never_a, 1, "rule"), (waiting_a, 2, "rule")]
