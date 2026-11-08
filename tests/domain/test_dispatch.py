"""Handing prints over to Bambuddy one by one (Spec 7.3, 7.7, plan B4 decisions 1-5)."""

import pytest
from sqlalchemy import func, select

from factories import make_article, make_dispatch, make_entry
from reprintbuddy.bambuddy.client import (
    BambuddyRejected,
    BambuddyUnavailable,
    BambuddyUnexpected,
)
from reprintbuddy.bambuddy.types import EnqueueRequest, QueueItem
from reprintbuddy.domain import dispatch
from reprintbuddy.domain import workqueue as wq
from reprintbuddy.models import Article, Dispatch, JournalEntry, WorkQueueEntry, utcnow


# ---- helpers ---------------------------------------------------------------------------


def article(s, name="Template A", *, file=1, plate=1, **fields):
    """Template A on file 1, plate 1, any A1 printer, unless told otherwise."""
    fields.setdefault("target_model", "A1")
    return make_article(s, name=name, library_file_id=file, plate_id=plate, **fields)


def entry(s, article_id, prints=1, **fields):
    """An entry at the end; positions 0 to n-1 as the work queue keeps them."""
    fields.setdefault("position", s.scalar(select(func.count()).select_from(WorkQueueEntry)))
    return make_entry(s, article_id, prints, **fields)


def entries_by_position(s):
    return list(s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position)))


def journal(s, kind):
    return list(
        s.scalars(select(JournalEntry).where(JournalEntry.kind == kind).order_by(JournalEntry.id))
    )


def queue_item(item_id, position, status="pending"):
    return QueueItem(
        id=item_id,
        status=status,
        batch_id=None,
        position=position,
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


async def hand(db, fake, *, cap=10, insert_next=False):
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=cap)
    assert res is not None
    return res, await dispatch.hand_over(fake, db, res, insert_next=insert_next)


# ---- reserving -------------------------------------------------------------------------


def test_reserve_takes_top_eligible(db):
    with db.begin() as s:
        a = article(s, "Template B", file=2, plate=1).id
        b = article(s, "Template C", file=2, plate=2, paused=True, pause_reason="unclear").id
        d = article(s, "Template D", file=3, plate=1, archived_at=utcnow()).id
        c = article(s).id
        ea = entry(s, a, held=True).id
        eb = entry(s, b).id
        ed = entry(s, d).id
        ec = entry(s, c)
        ec_id, ec_position = ec.id, ec.position
        res = dispatch.reserve_next(s, cap=10)
    assert res is not None and res.article_id == c
    assert ec_position == 3
    with db.begin() as s:
        assert s.get(WorkQueueEntry, ec_id) is None
        assert [e.id for e in wq.list_entries(s)] == [ea, eb, ed]
        assert [e.position for e in entries_by_position(s)] == [0, 1, 2]
        row = s.get(Dispatch, res.dispatch_id)
        assert row.status == "sending"
        assert row.units == 1
        assert row.enqueue_sent_at is None
        assert row.queue_position == ec_position
        assert row.queue_entry_id == ec_id
        assert row.origin == "rule"
        assert row.article_id == c
        assert not hasattr(res, "batch_name") and not hasattr(res, "external_ref")
        assert not hasattr(row, "external_ref") and not hasattr(row, "bambuddy_batch_id")  # gone with 0008 (p56)
        assert (res.library_file_id, res.plate_id, res.target_model, res.printer_id) == (
            1,
            1,
            "A1",
            None,
        )
        # skipping is not pausing
        assert not s.get(Article, a).paused
        assert journal(s, "automation_paused") == []


def test_reserve_respects_cap(db):
    with db.begin() as s:
        a = article(s).id
        make_dispatch(s, a, status="queued")
        make_dispatch(s, a, status="unclear")
        make_dispatch(s, a, status="completed")
        make_dispatch(s, a, status="returned")
        e = entry(s, a, 2).id
        assert dispatch.open_count(s) == 2
        assert dispatch.reserve_next(s, cap=2) is None
        assert s.get(WorkQueueEntry, e).prints == 2
        assert dispatch.open_count(s) == 2


def test_reserve_empty_queue(db):
    with db.begin() as s:
        assert dispatch.reserve_next(s, cap=10) is None


def test_reserve_no_target_pauses(db):
    with db.begin() as s:
        a = article(s, "Template B", file=2, plate=1, target_model=None).id
        c = article(s).id
        ea = entry(s, a).id
        entry(s, c)
        res = dispatch.reserve_next(s, cap=10)
        assert res is not None and res.article_id == c
        missing = s.get(Article, a)
        assert missing.paused and missing.pause_reason == "no_target"
        assert s.get(WorkQueueEntry, ea).prints == 1
        paused = journal(s, "automation_paused")
        assert len(paused) == 1 and paused[0].actor == "auto" and paused[0].article_id == a


def test_reserve_skips_waiting_for_printer(db):
    with db.begin() as s:
        a = article(s, "Template B", file=2, plate=1, target_model=None, printer_id=1).id
        b = article(s).id
        ea = entry(s, a).id
        entry(s, b)
        res = dispatch.reserve_next(s, cap=10, skip_article_ids={a})
        assert res is not None and res.article_id == b
        assert s.get(WorkQueueEntry, ea).prints == 1
        assert not s.get(Article, a).paused
        assert journal(s, "automation_paused") == []


# ---- handing over ----------------------------------------------------------------------


async def test_hand_over_queued(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s).id
        entry(s, a)
    res, result = await hand(db, fake_bambuddy)
    assert result == "queued"
    with db.begin() as s:
        row = s.get(Dispatch, res.dispatch_id)
        assert fake_bambuddy.enqueued[-1] == EnqueueRequest(1, 1, "A1", None, False, None)
        assert row.status == "queued"
        assert row.bambuddy_item_id is not None
        assert row.last_status == "pending"
        assert row.enqueue_sent_at is not None and row.dispatched_at is not None
        assert row.finished_at is None
        lines = journal(s, "print_dispatched")
        assert len(lines) == 1
        assert lines[0].actor == "auto" and lines[0].qty == 1 and lines[0].article_id == a
        assert lines[0].details == {"dispatch_id": row.id, "item_id": row.bambuddy_item_id}
        assert wq.list_entries(s) == []
    # Spec 7.6: one single entry, no batch is made or asked for.
    assert fake_bambuddy.calls == ["enqueue"]
    (item,) = await fake_bambuddy.list_all_items()
    assert item.batch_id is None


async def test_hand_over_printer_target(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s, target_model=None, printer_id=2).id
        entry(s, a)
    res, result = await hand(db, fake_bambuddy)
    assert result == "queued"
    req = fake_bambuddy.enqueued[-1]
    assert (req.target_model, req.printer_id) == (None, 2)
    assert res.target_model is None and res.printer_id == 2


async def test_entry_handed_print_by_print(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s, units_per_print=4).id
        e = entry(s, a, 3).id
    results = [await hand(db, fake_bambuddy) for _ in range(3)]
    assert [r for _, r in results] == ["queued"] * 3
    with db.begin() as s:
        assert s.get(WorkQueueEntry, e) is None
        rows = list(s.scalars(select(Dispatch).order_by(Dispatch.id)))
        assert [r.status for r in rows] == ["queued"] * 3
        assert [r.units for r in rows] == [4] * 3
        assert len({r.bambuddy_item_id for r in rows}) == 3
        assert dispatch.reserve_next(s, cap=10) is None
    assert len(fake_bambuddy.enqueued) == 3


def test_next_insert_position():
    assert dispatch.next_insert_position([], set()) == (True, None)
    queue = [queue_item(1, 1), queue_item(2, 2), queue_item(3, 3)]
    assert dispatch.next_insert_position(queue, {1, 2}) == (False, 3)
    assert dispatch.next_insert_position(queue, set()) == (True, None)
    assert dispatch.next_insert_position(queue, {99}) == (True, None)
    printing = [queue_item(5, None, "printing"), queue_item(3, 1)]
    assert dispatch.next_insert_position(printing, {5}) == (True, None)


async def test_insert_next_keeps_own_order(db, fake_bambuddy):
    f1 = fake_bambuddy.add_foreign_item("Other 1")
    f2 = fake_bambuddy.add_foreign_item("Other 2")
    with db.begin() as s:
        a = article(s).id
        entry(s, a, 2)
    r1, first = await hand(db, fake_bambuddy, insert_next=True)
    r2, second = await hand(db, fake_bambuddy, insert_next=True)
    assert (first, second) == ("queued", "queued")
    with db.begin() as s:
        i1 = s.get(Dispatch, r1.dispatch_id).bambuddy_item_id
        i2 = s.get(Dispatch, r2.dispatch_id).bambuddy_item_id
    assert [i.id for i in await fake_bambuddy.list_queue()] == [i1, i2, f1.id, f2.id]
    one, two = fake_bambuddy.enqueued
    assert (one.insert_at_top, one.insert_position) == (True, None)
    assert (two.insert_at_top, two.insert_position) == (False, 2)


async def test_insert_next_off_appends(db, fake_bambuddy):
    f1 = fake_bambuddy.add_foreign_item("Other 1")
    f2 = fake_bambuddy.add_foreign_item("Other 2")
    with db.begin() as s:
        a = article(s).id
        entry(s, a, 2)
    r1, _ = await hand(db, fake_bambuddy)
    r2, _ = await hand(db, fake_bambuddy)
    assert "list_queue" not in fake_bambuddy.calls
    for req in fake_bambuddy.enqueued:
        assert (req.insert_at_top, req.insert_position) == (False, None)
    with db.begin() as s:
        i1 = s.get(Dispatch, r1.dispatch_id).bambuddy_item_id
        i2 = s.get(Dispatch, r2.dispatch_id).bambuddy_item_id
    assert [i.id for i in await fake_bambuddy.list_queue()] == [f1.id, f2.id, i1, i2]


def test_confirm_dispatch_printing(db):
    with db.begin() as s:
        a = article(s).id
        row = make_dispatch(s, a, status="sending", enqueue_sent_at=utcnow())
        item = queue_item(11, None, "printing")
        dispatch.confirm_dispatch(s, row.id, item)
        assert row.status == "printing" and row.last_status == "printing"
        assert row.bambuddy_item_id == 11


# ---- failures --------------------------------------------------------------------------


@pytest.mark.parametrize("after_effect", [False, True])
@pytest.mark.parametrize(
    "exc", [BambuddyUnavailable("timeout"), BambuddyUnexpected("no JSON")], ids=["down", "odd"]
)
async def test_unavailable_on_enqueue_marks_unclear(db, fake_bambuddy, exc, after_effect):
    with db.begin() as s:
        a = article(s, stock=5).id
        e = entry(s, a, 2).id
    fake_bambuddy.fail_next("enqueue", exc, after_effect=after_effect)
    res, result = await hand(db, fake_bambuddy)
    assert result == "unclear"
    with db.begin() as s:
        row = s.get(Dispatch, res.dispatch_id)
        assert row.status == "unclear"
        assert row.enqueue_sent_at is not None
        assert row.bambuddy_item_id is None
        assert s.get(WorkQueueEntry, e).prints == 1  # not put back
        assert s.get(Article, a).stock == 5
        lines = journal(s, "print_dispatch_unclear")
        assert len(lines) == 1 and lines[0].actor == "auto" and lines[0].article_id == a
        assert lines[0].details == {"dispatch_id": row.id}
        assert journal(s, "print_returned") == []
        assert journal(s, "print_dispatched") == []
        assert dispatch.open_count(s) == 1
    assert fake_bambuddy.calls.count("enqueue") == 1


@pytest.mark.parametrize("method", ["list_queue"])
async def test_unavailable_before_enqueue_returns(db, fake_bambuddy, method):
    with db.begin() as s:
        a = article(s).id
        entry(s, a)
    fake_bambuddy.fail_next(method, BambuddyUnavailable("timeout"))
    res, result = await hand(db, fake_bambuddy, insert_next=True)
    assert result == "not_sent"
    with db.begin() as s:
        row = s.get(Dispatch, res.dispatch_id)
        assert row.status == "returned"
        assert row.message == "not_sent"
        assert row.enqueue_sent_at is None
        assert row.finished_at is not None
        entries = wq.list_entries(s)
        assert [(e.article_id, e.prints, e.origin) for e in entries] == [(a, 1, "rule")]
        lines = journal(s, "print_returned")
        assert len(lines) == 1 and lines[0].details["reason"] == "not_sent"
        assert lines[0].actor == "auto"
        assert dispatch.open_count(s) == 0
    assert "enqueue" not in fake_bambuddy.calls
    assert fake_bambuddy.enqueued == []


@pytest.mark.parametrize("method", ["list_queue", "enqueue"])
async def test_rejected_puts_print_back(db, fake_bambuddy, method):
    with db.begin() as s:
        a = article(s).id
        e = entry(s, a, 2).id
    fake_bambuddy.fail_next(method, BambuddyRejected("invalid", status=422))
    res, result = await hand(db, fake_bambuddy, insert_next=True)
    assert result == "rejected"
    with db.begin() as s:
        row = s.get(Dispatch, res.dispatch_id)
        assert row.status == "returned"
        assert row.message == "422: invalid"
        assert s.get(WorkQueueEntry, e).prints == 2
        lines = journal(s, "print_returned")
        assert len(lines) == 1 and lines[0].details["reason"] == "422: invalid"
        assert dispatch.open_count(s) == 0
    assert fake_bambuddy.enqueued == []


async def test_rejection_message_cut_to_500(db, fake_bambuddy):
    with db.begin() as s:
        entry(s, article(s).id)
    fake_bambuddy.fail_next("enqueue", BambuddyRejected("x" * 600, status=400))
    res, result = await hand(db, fake_bambuddy)
    assert result == "rejected"
    with db.begin() as s:
        assert len(s.get(Dispatch, res.dispatch_id).message) == 500


async def test_unknown_model_rejected_by_bambuddy(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s, target_model="X1").id
        entry(s, a)
    res, result = await hand(db, fake_bambuddy)
    assert result == "rejected"
    with db.begin() as s:
        row = s.get(Dispatch, res.dispatch_id)
        assert row.status == "returned"
        assert row.message == "400: No active printers for model: X1"
        assert [(e.article_id, e.prints) for e in wq.list_entries(s)] == [(a, 1)]


async def test_returned_last_print_recreated_in_place(db, fake_bambuddy):
    with db.begin() as s:
        x = article(s, target_model="X1").id
        y = article(s, "Template B", file=2, plate=1).id
        entry(s, x, origin="scrap")
        ey = entry(s, y).id
    res, result = await hand(db, fake_bambuddy)
    assert res.article_id == x and result == "rejected"
    with db.begin() as s:
        entries = wq.list_entries(s)
        assert [e.article_id for e in entries] == [x, y]
        assert entries[1].id == ey
        assert (entries[0].origin, entries[0].prints) == ("scrap", 1)
        assert [e.position for e in entries_by_position(s)] == [0, 1]


def test_return_ignores_reused_entry_id(db):
    """SQLite may give a deleted entry's id to a new one; the print must not land there."""
    with db.begin() as s:
        a = article(s).id
        b = article(s, "Template B", file=2, plate=1).id
        old = entry(s, a).id
        res = dispatch.reserve_next(s, cap=10)
    with db.begin() as s:
        other = entry(s, b, 2)
        assert other.id == old  # the case this test is about
        dispatch.return_print(s, res.dispatch_id, "not_sent")
        assert other.prints == 2
        entries = wq.list_entries(s)
        assert [(e.article_id, e.prints) for e in entries] == [(a, 1), (b, 2)]


@pytest.mark.parametrize(
    ("status", "sent"),
    [
        ("sending", True),  # crash or cancel after the mark: outcome unknown
        ("unclear", True),
        ("queued", True),
        ("printing", True),
        ("returned", False),
    ],
)
def test_return_refuses_print_that_may_be_in_bambuddy(db, status, sent):
    with db.begin() as s:
        a = article(s).id
        row = make_dispatch(s, a, status=status, enqueue_sent_at=utcnow() if sent else None)
        with pytest.raises(ValueError):
            dispatch.return_print(s, row.id, "not_sent")
        assert row.status == status
        assert wq.list_entries(s) == []


def test_return_sent_print_only_when_rejected(db):
    with db.begin() as s:
        a = article(s).id
        unclear = make_dispatch(s, a, status="unclear", enqueue_sent_at=utcnow())
        with pytest.raises(ValueError):
            dispatch.return_print(s, unclear.id, "422: invalid", rejected=True)
        row = make_dispatch(s, a, status="sending", enqueue_sent_at=utcnow())
        dispatch.return_print(s, row.id, "422: invalid", rejected=True)
        assert row.status == "returned" and row.message == "422: invalid"
        assert [(e.article_id, e.prints) for e in wq.list_entries(s)] == [(a, 1)]


def test_mark_sent_only_once(db):
    with db.begin() as s:
        a = article(s).id
        row = make_dispatch(s, a, status="sending")
        dispatch.mark_sent(s, row.id)
        first = row.enqueue_sent_at
        assert first is not None
        with pytest.raises(ValueError):
            dispatch.mark_sent(s, row.id)
        assert row.enqueue_sent_at == first


async def test_sent_mark_committed_and_no_transaction_during_calls(db, fake_bambuddy):
    """Every Bambuddy call runs outside a transaction; `enqueue_sent_at` is committed first."""
    with db.begin() as s:
        entry(s, article(s).id)
    seen = []

    def wrap(name):
        original = getattr(fake_bambuddy, name)

        async def call(*args):
            # A write transaction of hand_over would block this one ("database is locked").
            with db.begin() as s:
                row = s.scalars(select(Dispatch)).one()
                seen.append((name, row.status, row.enqueue_sent_at is not None))
            return await original(*args)

        setattr(fake_bambuddy, name, call)

    for name in ("list_queue", "enqueue"):
        wrap(name)
    _, result = await hand(db, fake_bambuddy, insert_next=True)
    assert result == "queued"
    assert seen == [
        ("list_queue", "sending", False),
        ("enqueue", "sending", True),
    ]


async def test_only_allowed_bambuddy_calls(db, fake_bambuddy):
    fake_bambuddy.add_foreign_item("Other 1")
    with db.begin() as s:
        a = article(s).id
        entry(s, a, 9)
    await hand(db, fake_bambuddy)
    await hand(db, fake_bambuddy, insert_next=True)
    fake_bambuddy.fail_next("enqueue", BambuddyUnavailable("timeout"))
    await hand(db, fake_bambuddy)
    fake_bambuddy.fail_next("list_queue", BambuddyUnavailable("timeout"))
    await hand(db, fake_bambuddy, insert_next=True)
    fake_bambuddy.fail_next("enqueue", BambuddyRejected("invalid", status=422))
    await hand(db, fake_bambuddy, insert_next=True)
    assert set(fake_bambuddy.calls) == {"list_queue", "enqueue"}


async def test_update_before_send_puts_print_back(db, fake_bambuddy):
    """Final review 1: an update that starts after the reservation stops the send."""
    with db.begin() as s:
        a = article(s).id
        e = entry(s, a, 2).id
    asked = []

    def may_send():
        asked.append(list(fake_bambuddy.calls))
        return False

    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    result = await dispatch.hand_over(
        fake_bambuddy, db, res, insert_next=False, may_send=may_send
    )

    assert result == "held_for_update"
    assert asked == [[]]  # asked right before the mark, before any write
    assert "enqueue" not in fake_bambuddy.calls
    assert fake_bambuddy.enqueued == []
    with db.begin() as s:
        row = s.get(Dispatch, res.dispatch_id)
        assert row.status == "returned"
        assert row.message == "not_sent"
        assert row.enqueue_sent_at is None
        assert s.get(WorkQueueEntry, e).prints == 2
        assert dispatch.open_count(s) == 0
