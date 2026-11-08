"""Settling unclear hand-overs without ever printing twice (Spec 7.6, 7.7, plan B4 decision 5).

Review focus 1 and 3: a print whose `enqueue` was sent is searched in Bambuddy and adopted;
when nothing is found the article pauses and nothing is sent again. Only a print that was
certainly never sent goes back to the work queue.

Since v0.3.5 (Spec 7.6) every print is a single queue entry. The search reads Bambuddy's
whole queue once and looks, among the entries Reprintbuddy does not know yet, for its own
one: own Bambuddy user, same file and plate, made after the send (less the clock
tolerance). Exactly one hit is adopted; none or several leave the print `missing` and the
article paused, never guessed and never sent again.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from factories import make_article, make_dispatch, make_entry
from reprintbuddy import settings as st
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
    Setting,
    WorkQueueEntry,
    utcnow,
)

T0 = datetime(2026, 11, 7, 8, 0, 0)  # naive UTC, like `enqueue_sent_at` and `created_at`


@pytest.fixture(autouse=True)
def own_user(db, fake_bambuddy):
    """Reprintbuddy's Bambuddy user is the fake's (set by the connection check in the app)."""
    with db() as s:
        st.set_setting(s, "bambuddy_username", fake_bambuddy.username)


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
    """Reserved, `enqueue_sent_at` committed - then the process died before `enqueue`."""
    with db.begin() as s:
        entry(s, a)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    with db.begin() as s:
        dispatch.mark_sent(s, res.dispatch_id)
    return res.dispatch_id


def sent_at(db, dispatch_id):
    with db() as s:
        return s.get(Dispatch, dispatch_id).enqueue_sent_at


def set_sent_at(db, dispatch_id, when):
    with db.begin() as s:
        s.get(Dispatch, dispatch_id).enqueue_sent_at = when


def own_entry(fake, *, file=1, plate=1, created_at=None, status="pending", user=None, **kw):
    """A Bambuddy entry made outside `enqueue`: by default Reprintbuddy's own for file 1/plate 1."""
    return fake.add_foreign_item(
        "Template A.gcode.3mf",
        status=status,
        created_by=fake.username if user is None else user,
        library_file_id=file,
        plate_id=plate,
        created_at=created_at,
        **kw,
    )


async def the_only_item(fake):
    (item,) = await fake.list_all_items()
    return item


# ---- found: adopt, never send again ----------------------------------------------------


async def test_lost_response_found_not_resent(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    found = await the_only_item(fake_bambuddy)
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report == dispatch.ResolveReport(
        reachable=True, resolved=1, unresolved=0, duplicates=[], lost=[]
    )
    assert len(fake_bambuddy.enqueued) == 1
    assert fake_bambuddy.calls == ["list_all_items"]
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "queued"
        assert row.bambuddy_item_id == found.id
        assert row.last_status == "pending"
        assert row.dispatched_at is not None and row.finished_at is None
        (line,) = journal(s, "print_dispatched")
        assert line.actor == "auto" and line.article_id == a and line.qty == 1
        assert line.details == {"dispatch_id": d, "item_id": found.id}
        assert not s.get(Article, a).paused
        assert dispatch.open_count(s) == 1
    # Settled: a second run has nothing left to search.
    fake_bambuddy.calls.clear()
    again = await dispatch.resolve_pending(fake_bambuddy, db)
    assert again == dispatch.ResolveReport(True, 0, 0, [], [])
    assert fake_bambuddy.calls == []


async def test_resolved_item_already_finished(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    found = await the_only_item(fake_bambuddy)
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
    found = await the_only_item(fake_bambuddy)
    fake_bambuddy.set_status(found.id, "paused")

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "queued" and row.last_status == "paused"
        assert row.bambuddy_item_id == found.id and row.finished_at is None


async def test_one_candidate_among_others_is_adopted(db, fake_bambuddy):
    """Entries that are no candidate do not make the search unclear."""
    a, d = await lost_answer(db, fake_bambuddy, effect=False)
    when = sent_at(db, d)
    real = own_entry(fake_bambuddy, created_at=when + timedelta(seconds=2))
    own_entry(fake_bambuddy, created_at=when + timedelta(seconds=3), user="admin")
    own_entry(fake_bambuddy, created_at=when + timedelta(seconds=4), plate=2)
    own_entry(fake_bambuddy, created_at=when - timedelta(minutes=10))

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1 and report.lost == [] and report.duplicates == []
    with db() as s:
        assert s.get(Dispatch, d).bambuddy_item_id == real.id


async def test_entry_within_the_clock_tolerance_is_adopted(db, fake_bambuddy):
    """Bambuddy's clock may run behind: an entry up to 5 minutes "before" the send counts."""
    a, d = await lost_answer(db, fake_bambuddy, effect=False)
    early = own_entry(fake_bambuddy, created_at=sent_at(db, d) - timedelta(minutes=4, seconds=59))

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1
    with db() as s:
        assert s.get(Dispatch, d).bambuddy_item_id == early.id


NOT_CANDIDATES = {
    "other-user": dict(user="admin"),
    "no-user": dict(user=""),
    "other-file": dict(file=2),
    "other-plate": dict(plate=2),
    "before-window": dict(before=timedelta(minutes=5, seconds=1)),
    "no-created-at": dict(undated=True),
    "known-id": dict(known=True),
}


@pytest.mark.parametrize("case", NOT_CANDIDATES.values(), ids=NOT_CANDIDATES.keys())
async def test_entry_that_is_no_candidate_is_not_adopted(db, fake_bambuddy, case):
    case = dict(case)
    a, d = await lost_answer(db, fake_bambuddy, effect=False)
    when = sent_at(db, d)
    before = case.pop("before", None)
    known = case.pop("known", False)
    created = when - before if before else when + timedelta(seconds=1)
    if case.pop("undated", False):
        item = own_entry(fake_bambuddy, undated=True, **case)
    else:
        item = own_entry(fake_bambuddy, created_at=created, **case)
    if known:  # already the entry of another print in Reprintbuddy
        with db.begin() as s:
            make_dispatch(s, a, status="completed", bambuddy_item_id=item.id)

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.lost == [d] and report.resolved == 0 and report.duplicates == []
    with db() as s:
        row = s.get(Dispatch, d)
        assert row.status == "missing" and row.bambuddy_item_id is None
        assert s.get(Article, a).pause_reason == "unclear"
    assert fake_bambuddy.enqueued == []


async def test_older_own_entry_in_the_window_makes_it_ambiguous(db, fake_bambuddy):
    """Spec 7.6: an unknown own entry from shortly before the send plus the real one: no
    guess, no second send - `missing`, pause `unclear`, journal and warning with both ids."""
    a, d = await lost_answer(db, fake_bambuddy)
    real = await the_only_item(fake_bambuddy)
    older = own_entry(fake_bambuddy, created_at=sent_at(db, d) - timedelta(minutes=3))
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    ids = sorted([older.id, real.id])
    assert report.duplicates == [ids]
    assert report.duplicate_articles == {older.id: a, real.id: a}
    assert report.resolved == 0 and report.lost == [] and report.unresolved == 0
    assert fake_bambuddy.calls == ["list_all_items"]
    assert len(fake_bambuddy.enqueued) == 1
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "missing" and row.finished_at is not None
        assert row.bambuddy_item_id is None  # none adopted
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "unclear"
        (paused,) = journal(s, "automation_paused")
        assert paused.actor == "auto" and paused.article_id == a
        assert paused.details["item_ids"] == ids
        assert paused.details["dispatch_id"] == d
        assert paused.details["reason"] == "unclear"
        (missing,) = journal(s, "print_missing")
        assert missing.details == {"dispatch_id": d, "reason": "ambiguous", "item_ids": ids}
        (dup,) = journal(s, "print_duplicates")
        assert dup.actor == "auto" and dup.article_id == a
        assert dup.details == {"dispatch_id": d, "item_ids": ids}
        assert journal(s, "print_dispatched") == []
        assert queue_of(s) == []
        assert dispatch.open_count(s) == 0
    # Settled for good: nothing is searched or sent on the next run.
    fake_bambuddy.calls.clear()
    assert await dispatch.resolve_pending(fake_bambuddy, db) == dispatch.ResolveReport(
        True, 0, 0, [], []
    )
    assert fake_bambuddy.calls == []


async def test_ambiguous_journaled_when_article_already_paused(db, fake_bambuddy):
    """Final review 2: the ids are written down even when the pause writes nothing."""
    from reprintbuddy.domain.pauses import pause_article

    a, d = await lost_answer(db, fake_bambuddy)
    real = await the_only_item(fake_bambuddy)
    second = own_entry(fake_bambuddy, created_at=sent_at(db, d) + timedelta(seconds=5))
    with db.begin() as s:
        pause_article(s, a, "two_failures", "auto")

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.duplicates == [[real.id, second.id]]
    with db.begin() as s:
        assert s.get(Article, a).pause_reason == "two_failures"
        assert len(journal(s, "automation_paused")) == 1  # only the earlier pause
        (dup,) = journal(s, "print_duplicates")
        assert dup.article_id == a
        assert dup.details == {"dispatch_id": d, "item_ids": [real.id, second.id]}
        assert s.get(Dispatch, d).status == "missing"


async def test_two_unclear_prints_of_one_article_far_apart(db, fake_bambuddy):
    """Oldest first, by the rule as it stands: the older print sees both entries (the later
    one was made after its send too) and pauses; the later print sees only its own."""
    with db.begin() as s:
        a = article(s).id
        first = make_dispatch(s, a, status="unclear", enqueue_sent_at=T0).id
        later = make_dispatch(
            s, a, status="unclear", enqueue_sent_at=T0 + timedelta(minutes=10)
        ).id
    one = own_entry(fake_bambuddy, created_at=T0 + timedelta(seconds=1))
    two = own_entry(fake_bambuddy, created_at=T0 + timedelta(minutes=10, seconds=1))

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1 and report.lost == []
    assert report.duplicates == [[one.id, two.id]]
    assert fake_bambuddy.calls == ["list_all_items"]  # one read for every print
    with db() as s:
        row = s.get(Dispatch, first)
        assert row.status == "missing" and row.bambuddy_item_id is None
        assert s.get(Dispatch, later).bambuddy_item_id == two.id
        assert s.get(Article, a).pause_reason == "unclear"
    assert fake_bambuddy.enqueued == []


async def test_older_print_found_later_print_never_arrived(db, fake_bambuddy):
    """The older print's entry is adopted first; the later print (crashed after the mark,
    never in Bambuddy) does not take it and counts as lost."""
    with db.begin() as s:
        a = article(s).id
        first = make_dispatch(s, a, status="unclear", enqueue_sent_at=T0).id
        later = make_dispatch(
            s, a, status="sending", enqueue_sent_at=T0 + timedelta(seconds=30)
        ).id
    one = own_entry(fake_bambuddy, created_at=T0 + timedelta(seconds=1))

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1 and report.lost == [later] and report.duplicates == []
    with db() as s:
        assert s.get(Dispatch, first).bambuddy_item_id == one.id
        assert s.get(Dispatch, later).status == "missing"


async def test_two_unclear_prints_of_one_article_close_together(db, fake_bambuddy):
    """Both entries lie in both windows: nothing can be told apart, nothing is adopted."""
    with db.begin() as s:
        a = article(s).id
        first = make_dispatch(s, a, status="unclear", enqueue_sent_at=T0).id
        later = make_dispatch(
            s, a, status="unclear", enqueue_sent_at=T0 + timedelta(minutes=1)
        ).id
    one = own_entry(fake_bambuddy, created_at=T0 + timedelta(seconds=1))
    two = own_entry(fake_bambuddy, created_at=T0 + timedelta(minutes=1, seconds=1))

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 0 and report.lost == []
    assert report.duplicates == [[one.id, two.id], [one.id, two.id]]
    with db() as s:
        for d in (first, later):
            row = s.get(Dispatch, d)
            assert row.status == "missing" and row.bambuddy_item_id is None
        assert s.get(Article, a).pause_reason == "unclear"
        assert len(journal(s, "print_duplicates")) == 2
    assert fake_bambuddy.enqueued == []


def test_adopt_writes_dispatched_once(db, fake_bambuddy):
    """A print adopted after an earlier journal line keeps that single line."""
    with db.begin() as s:
        a = article(s).id
        row = make_dispatch(s, a, status="unclear", enqueue_sent_at=utcnow())
        write_journal(
            s, kind="print_dispatched", actor="auto", qty=1, article_id=a,
            details={"dispatch_id": row.id, "item_id": 9},
        )
        other = make_dispatch(s, a, status="queued", bambuddy_item_id=8)
        write_journal(
            s, kind="print_dispatched", actor="auto", qty=1, article_id=a,
            details={"dispatch_id": other.id, "item_id": 8},
        )
        item = own_entry(fake_bambuddy, status="printing")
        dispatch.adopt(s, row.id, item)
        assert row.status == "printing" and row.bambuddy_item_id == item.id
        assert len(journal(s, "print_dispatched")) == 2


def test_adopt_refuses_settled_print(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s).id
        row = make_dispatch(s, a, status="missing", enqueue_sent_at=utcnow())
        with pytest.raises(ValueError):
            dispatch.adopt(s, row.id, own_entry(fake_bambuddy))
        assert row.status == "missing" and row.bambuddy_item_id is None


# ---- sent but not found: pause, never send again ---------------------------------------


async def test_sent_but_not_found_pauses(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy, prints=2, effect=False)
    with db.begin() as s:
        before = queue_of(s)
    assert before == [(a, 1, "rule")]
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report == dispatch.ResolveReport(
        reachable=True, resolved=0, unresolved=0, duplicates=[], lost=[d]
    )
    assert fake_bambuddy.calls == ["list_all_items"]
    assert fake_bambuddy.enqueued == []
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "missing"
        assert row.finished_at is not None
        assert row.bambuddy_item_id is None
        (line,) = journal(s, "print_missing")
        assert line.actor == "auto" and line.article_id == a and line.qty == 1
        assert line.details == {"dispatch_id": d, "reason": "not_found_after_send"}
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
    found = await the_only_item(fake_bambuddy)
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
    d = await crashed_after_mark(db, fake_bambuddy, a)
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "sending"
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.lost == [d] and report.unresolved == 0
    assert fake_bambuddy.calls == ["list_all_items"]
    assert fake_bambuddy.enqueued == []
    with db.begin() as s:
        row = s.get(Dispatch, d)
        assert row.status == "missing"
        (line,) = journal(s, "print_missing")
        assert line.details == {"dispatch_id": d, "reason": "not_found_after_send"}
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "unclear"
        assert queue_of(s) == []


def test_mark_lost_refuses_unsent_or_settled(db):
    with db.begin() as s:
        a = article(s).id
        unsent = make_dispatch(s, a, status="sending")
        settled = make_dispatch(s, a, status="queued", enqueue_sent_at=utcnow())
        for row in (unsent, settled):
            with pytest.raises(ValueError):
                dispatch.mark_lost(s, row.id)
            with pytest.raises(ValueError):
                dispatch.mark_lost(s, row.id, item_ids=[1, 2])
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
        first = dispatch.reserve_next(s, cap=10)  # then cancelled before the mark
    with db.begin() as s:
        second = dispatch.reserve_next(s, cap=10)
        assert queue_of(s) == []
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert fake_bambuddy.calls == []
    assert report == dispatch.ResolveReport(True, 0, 0, [], [])
    with db.begin() as s:
        for res in (first, second):
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


# ---- Bambuddy unreachable or refusing, own user unknown: stay unclear ------------------


async def test_resolve_unavailable_keeps_unclear(db, fake_bambuddy):
    a, d = await lost_answer(db, fake_bambuddy)
    d2 = await crashed_after_mark(db, fake_bambuddy, a)
    fake_bambuddy.calls.clear()
    fake_bambuddy.unavailable = True

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report == dispatch.ResolveReport(
        reachable=False, resolved=0, unresolved=2, duplicates=[], lost=[]
    )
    # One failed read; nothing is sent.
    assert fake_bambuddy.calls == ["list_all_items"]
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
    d2 = await crashed_after_mark(db, fake_bambuddy, a)
    fake_bambuddy.calls.clear()
    fake_bambuddy.fail_next("list_all_items", BambuddyRejected("bad", status=422))

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    # Final review 8: the refusal's text is kept for the warning.
    assert report == dispatch.ResolveReport(
        reachable=True, resolved=0, unresolved=2, duplicates=[], lost=[], error="422: bad"
    )
    assert fake_bambuddy.calls == ["list_all_items"]
    with db.begin() as s:
        assert s.get(Dispatch, d).status == "unclear"
        assert s.get(Dispatch, d2).status == "unclear"
        assert journal(s, "print_missing") == []
        assert not s.get(Article, a).paused


@pytest.mark.parametrize("raw", ["null", '""', '"  "'], ids=["none", "empty", "blank"])
async def test_unknown_own_user_keeps_unclear(db, fake_bambuddy, raw):
    """Without its own Bambuddy user Reprintbuddy cannot tell its entry: nothing is decided."""
    a, d = await lost_answer(db, fake_bambuddy)
    with db() as s:
        # `set_setting` refuses an empty name; the raw row stands for older or odd data.
        s.merge(Setting(key="bambuddy_username", value=raw))
        s.commit()
    fake_bambuddy.calls.clear()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.unresolved == 1 and report.resolved == 0 and report.lost == []
    assert report.reachable is True
    assert report.error == dispatch.NO_OWN_USER
    assert fake_bambuddy.calls == []  # nothing to search with
    with db() as s:
        row = s.get(Dispatch, d)
        assert row.status == "unclear" and row.bambuddy_item_id is None
        assert journal(s, "print_missing") == []
        assert not s.get(Article, a).paused
    assert len(fake_bambuddy.enqueued) == 1


# ---- resolve_pending never enqueues ----------------------------------------------------


async def test_resolve_never_enqueues(db, fake_bambuddy):
    """Every case at once: resolve only searches, the work queue keeps its prints."""
    found_a, found_d = await lost_answer(db, fake_bambuddy, plate=1)  # found, waiting
    _, gone_d = await lost_answer(db, fake_bambuddy, effect=False, plate=2)  # sent, not found
    _, done_d = await lost_answer(db, fake_bambuddy, plate=3)  # found, completed
    done = [i for i in await fake_bambuddy.list_all_items() if i.plate_id == 3][0]
    fake_bambuddy.set_status(done.id, "completed")
    _, dup_d = await lost_answer(db, fake_bambuddy, plate=4)  # found twice
    await fake_bambuddy.enqueue(EnqueueRequest(1, 4, "A1", None, False, None))
    with db.begin() as s:
        crash_a = article(s, "Template 5", plate=5).id
    crash_d = await crashed_after_mark(db, fake_bambuddy, crash_a)  # marked, never sent
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

    assert fake_bambuddy.calls == ["list_all_items"]
    assert len(fake_bambuddy.enqueued) == enqueued_before
    assert report.reachable is True and report.unresolved == 0
    assert report.resolved == 2
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
            dup_d: "missing",
            crash_d: "missing",
            never.dispatch_id: "returned",
        }
        # Only the never-sent print went back; nothing else was added.
        assert queue_of(s) == [(never_a, 1, "rule"), (waiting_a, 2, "rule")]
