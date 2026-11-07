"""Inspecting finished prints: the count of good units books stock and replaces scrap
(plan B5 task 1; Spec 7.5, 8.2, 8.3, 12 step 6)."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from factories import make_article, make_dispatch, make_entry, make_part
from reprintbuddy.domain import inspection, stock, tracking
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.domain.stock import PartShortage
from reprintbuddy.models import (
    Article,
    ArticlePart,
    Inspection,
    JournalEntry,
    Part,
    WorkQueueEntry,
    utcnow,
)


# ---- helpers ---------------------------------------------------------------------------


def finished_print(s, article_id, units=1):
    """A queued print that Bambuddy reports as completed: provisional units + open inspection."""
    dispatch = make_dispatch(s, article_id, status="queued", units=units)
    assert tracking.apply_outcome(s, dispatch.id, "completed", "completed")
    return s.scalar(select(Inspection).where(Inspection.dispatch_id == dispatch.id)), dispatch


def journal(s, kind):
    return list(
        s.scalars(select(JournalEntry).where(JournalEntry.kind == kind).order_by(JournalEntry.id))
    )


def queue(s):
    return list(s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position)))


def counters(s, article_id):
    a = s.get(Article, article_id)
    return a.provisional, a.stock, a.awaiting_assembly


def with_insert(s, article_id, stock_=50):
    insert = make_part(s, name="Insert M4", stock=stock_)
    s.add(ArticlePart(article_id=article_id, part_id=insert.id, qty_per_unit=2))
    s.flush()
    return insert


# ---- the list --------------------------------------------------------------------------


def test_list_open_oldest_first(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", needs_assembly=True)
        b = make_article(s, name="Template B", thumbnail=b"png")
        ids = []
        for article in (b, a, b):
            row, _ = finished_print(s, article.id, units=2)
            ids.append(row.id)
        newest, oldest, middle = ids[0], ids[1], ids[2]
        now = utcnow()
        s.get(Inspection, oldest).created_at = now - timedelta(hours=2)
        s.get(Inspection, middle).created_at = now - timedelta(hours=1)
        s.get(Inspection, newest).created_at = now
        done, _ = finished_print(s, b.id)
        done.created_at = now - timedelta(hours=5)
        s.flush()
        inspection.resolve(s, done.id, 1, "user")
    with db.begin() as s:
        rows = inspection.list_open(s)
        assert [r.id for r in rows] == [oldest, middle, newest]
        first = rows[0]
        assert (first.article_id, first.article_name, first.units) == (a.id, "Template A", 2)
        assert first.needs_assembly is True and first.has_thumbnail is False
        assert rows[1].needs_assembly is False and rows[1].has_thumbnail is True
        assert isinstance(first, inspection.InspectionRow)


def test_list_open_same_time_by_id(db):
    with db.begin() as s:
        a = make_article(s).id
        first, _ = finished_print(s, a)
        second, _ = finished_print(s, a)
        same = utcnow()
        first.created_at = second.created_at = same
        ids = [first.id, second.id]
    with db.begin() as s:
        assert [r.id for r in inspection.list_open(s)] == ids


# ---- resolve ---------------------------------------------------------------------------


def test_resolve_all_good_no_assembly(db):  # Spec 8.2, 6.2
    with db.begin() as s:
        a = make_article(s)
        insert = with_insert(s, a.id)
        row, dispatch = finished_print(s, a.id, units=1)
        ids = (a.id, insert.id, row.id, dispatch.id)
        assert counters(s, a.id) == (1, 0, 0)
    with db.begin() as s:
        result = inspection.resolve(s, ids[2], 1, "user", note="looks fine")
        assert result == inspection.ResolveResult(
            inspection_id=ids[2], good=1, scrap=0, shortages=[], replacement_entry_id=None
        )
    with db.begin() as s:
        assert counters(s, ids[0]) == (0, 1, 0)
        assert s.get(Part, ids[1]).stock == 48
        row = s.get(Inspection, ids[2])
        assert row.good_units == 1 and row.resolved_at is not None
        assert queue(s) == []
        good = journal(s, "inspection_good")
        assert len(good) == 1
        assert good[0].note == "looks fine" and good[0].actor == "user"
        assert good[0].details["inspection_id"] == ids[2]
        assert good[0].details["dispatch_id"] == ids[3]
        assert journal(s, "inspection_scrap") == []


def test_resolve_good_with_assembly(db):  # Spec 8.3
    with db.begin() as s:
        a = make_article(s, needs_assembly=True)
        insert = with_insert(s, a.id)
        row, _ = finished_print(s, a.id)
        ids = (a.id, insert.id, row.id)
    with db.begin() as s:
        inspection.resolve(s, ids[2], 1, "user")
    with db.begin() as s:
        assert counters(s, ids[0]) == (0, 0, 1)
        assert s.get(Part, ids[1]).stock == 50


def test_resolve_with_scrap_replaces_at_start(db):  # Spec 7.5, 12 step 6
    with db.begin() as s:
        a = make_article(s)
        existing = make_entry(s, a.id, 3, position=0)
        row, dispatch = finished_print(s, a.id, units=1)
        ids = (a.id, existing.id, row.id, dispatch.id)
    with db.begin() as s:
        result = inspection.resolve(s, ids[2], 0, "user")
        assert (result.good, result.scrap) == (0, 1)
        assert result.replacement_entry_id is not None
        replacement_id = result.replacement_entry_id
    with db.begin() as s:
        assert counters(s, ids[0]) == (0, 0, 0)
        scrap = journal(s, "inspection_scrap")
        assert len(scrap) == 1 and scrap[0].qty == 1
        assert scrap[0].details["inspection_id"] == ids[2]
        assert scrap[0].details["dispatch_id"] == ids[3]
        assert journal(s, "inspection_good") == []
        entries = queue(s)
        assert [e.id for e in entries] == [replacement_id, ids[1]]
        assert (entries[0].origin, entries[0].prints, entries[0].article_id) == ("scrap", 1, ids[0])
        created = [
            j for j in journal(s, "queue_entry_created") if j.details["entry_id"] == replacement_id
        ]
        assert created[0].details["inspection_id"] == ids[2]
        assert created[0].details["dispatch_id"] == ids[3]


def test_scrap_rounds_up_to_prints(db):  # Review Focus 4
    with db.begin() as s:
        a = make_article(s, units_per_print=4)
        row, _ = finished_print(s, a.id, units=4)
        ids = (a.id, row.id)
    with db.begin() as s:
        result = inspection.resolve(s, ids[1], 1, "user")
        assert (result.good, result.scrap) == (1, 3)
    with db.begin() as s:
        assert counters(s, ids[0]) == (0, 1, 0)
        (entry,) = queue(s)
        assert entry.prints == 1  # ceil(3 / 4)


def test_scrap_uses_current_units_per_print(db):  # plan B5 decision 2
    with db.begin() as s:
        a = make_article(s, units_per_print=4)
        row, _ = finished_print(s, a.id, units=4)
        ids = (a.id, row.id)
    with db.begin() as s:
        s.get(Article, ids[0]).units_per_print = 2
    with db.begin() as s:
        inspection.resolve(s, ids[1], 1, "user")
    with db.begin() as s:
        (entry,) = queue(s)
        assert entry.prints == 2  # ceil(3 / 2)


def test_scrap_replacement_when_automation_off_and_paused(db):  # Spec 7.5
    with db.begin() as s:
        a = make_article(s, auto_enabled=False, paused=True, pause_reason="manual")
        row, _ = finished_print(s, a.id, units=1)
        ids = (a.id, row.id)
        assert s.get(Article, a.id).auto_enabled is False
    with db.begin() as s:
        result = inspection.resolve(s, ids[1], 0, "user")
        assert result.replacement_entry_id is not None
    with db.begin() as s:
        (entry,) = queue(s)
        assert (entry.origin, entry.prints, entry.article_id) == ("scrap", 1, ids[0])


@pytest.mark.parametrize("bad", [-1, "units+1", True, 1.5])
def test_resolve_rejects_out_of_range(db, bad):  # Review Focus 3
    with db.begin() as s:
        a = make_article(s)
        row, _ = finished_print(s, a.id, units=2)
        ids = (a.id, row.id)
    value = 3 if bad == "units+1" else bad
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            inspection.resolve(s, ids[1], value, "user")
    assert info.value.key == "form.invalid_number"
    assert info.value.params["field"] == "inspections.field.good"
    assert (info.value.params["min"], info.value.params["max"]) == (0, 2)
    with db.begin() as s:
        assert counters(s, ids[0]) == (2, 0, 0)
        row = s.get(Inspection, ids[1])
        assert row.good_units is None and row.resolved_at is None
        assert journal(s, "inspection_good") == [] and journal(s, "inspection_scrap") == []
        assert queue(s) == []


def test_resolve_twice_conflict(db):  # Review Focus 1, 2
    with db.begin() as s:
        a = make_article(s)
        row, _ = finished_print(s, a.id)
        ids = (a.id, row.id)
    with db.begin() as s:
        inspection.resolve(s, ids[1], 1, "user")
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            inspection.resolve(s, ids[1], 1, "user")
    assert info.value.key == "inspections.already_resolved"
    with db.begin() as s:
        assert counters(s, ids[0]) == (0, 1, 0)
        assert len(journal(s, "inspection_good")) == 1


def test_resolve_unknown(db):
    with pytest.raises(NotFoundError) as info:
        with db.begin() as s:
            inspection.resolve(s, 999, 1, "user")
    assert info.value.key == "inspections.not_found"


def test_resolve_short_parts(db):  # B3 decision 5
    with db.begin() as s:
        a = make_article(s)
        insert = with_insert(s, a.id, stock_=1)
        row, _ = finished_print(s, a.id)
        ids = (a.id, insert.id, row.id)
    with db.begin() as s:
        result = inspection.resolve(s, ids[2], 1, "user")
        assert result.shortages == [PartShortage(ids[1], 2, 1)]
    with db.begin() as s:
        assert s.get(Part, ids[1]).stock == 0
        assert counters(s, ids[0]) == (0, 1, 0)
        assert s.get(Inspection, ids[2]).resolved_at is not None


# ---- all good --------------------------------------------------------------------------


def test_resolve_all_good(db):
    with db.begin() as s:
        a = make_article(s)
        insert = with_insert(s, a.id, stock_=10)
        rows = [finished_print(s, a.id, units=2)[0].id for _ in range(3)]
        inspection.resolve(s, rows[1], 1, "user")
        ids = (a.id, insert.id, *rows)
    with db.begin() as s:
        result = inspection.resolve_all_good(s, [ids[2], ids[3], ids[4], 999], "user")
        assert isinstance(result, inspection.AllGoodResult)
        assert (result.resolved, result.skipped) == (2, 2)  # the done one and the unknown one
        assert result.shortages == []
    with db.begin() as s:
        # first and third: 2 good each; second: 1 good before (and one scrap, replaced)
        assert counters(s, ids[0]) == (0, 5, 0)
        assert s.get(Part, ids[1]).stock == 10 - 2 * 5
        assert s.get(Inspection, ids[2]).good_units == 2
        assert s.get(Inspection, ids[3]).good_units == 1  # untouched by "all good"
        assert s.get(Inspection, ids[4]).good_units == 2


def test_resolve_all_good_collects_shortages(db):
    with db.begin() as s:
        a = make_article(s)
        insert = with_insert(s, a.id, stock_=3)
        rows = [finished_print(s, a.id, units=1)[0].id for _ in range(2)]
        ids = (a.id, insert.id, *rows)
    with db.begin() as s:
        result = inspection.resolve_all_good(s, ids[2:], "user")
        assert (result.resolved, result.skipped) == (2, 0)
        assert result.shortages == [PartShortage(ids[1], 2, 1)]  # the second one is short
    with db.begin() as s:
        assert s.get(Part, ids[1]).stock == 0


# ---- two requests at once --------------------------------------------------------------


def test_concurrent_resolve_books_once(db):  # Review Focus 1
    with db.begin() as s:
        a = make_article(s)
        row, _ = finished_print(s, a.id)
        ids = (a.id, row.id)
    barrier, results = threading.Barrier(2), []

    def submit():
        barrier.wait()
        try:
            with db.begin() as s:
                inspection.resolve(s, ids[1], 1, "user")
            results.append("ok")
        except ConflictError as e:
            results.append(e.key)
        except Exception as e:  # anything else fails the test
            results.append(repr(e))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["inspections.already_resolved", "ok"]
    with db.begin() as s:
        assert counters(s, ids[0]) == (0, 1, 0)
        assert len(journal(s, "inspection_good")) == 1
