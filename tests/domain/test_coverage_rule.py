from datetime import date

import pytest
from sqlalchemy import select

from factories import make_article, make_dispatch, make_entry
from reprintbuddy import settings as st
from reprintbuddy.domain import orders, workqueue
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.domain.workqueue import Coverage
from reprintbuddy.models import JournalEntry, WorkQueueEntry


def automation(s, on=True):
    st.set_setting(s, "automation", on, commit=False)


def entries(s):
    return list(s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position)))


def test_coverage_counts_everything(db):
    with db.begin() as s:
        a = make_article(s, stock=3, reserved=1, provisional=1, awaiting_assembly=1)
        make_entry(s, a.id, 2)
        for status in ("sending", "unclear", "queued", "printing"):
            make_dispatch(s, a.id, status=status)
        for status in ("completed", "failed", "cancelled", "missing", "returned"):
            make_dispatch(s, a.id, status=status)
        c = workqueue.coverage_of(s, a)
        assert c == Coverage(2, 2, 4, 1, 1)
        assert c.total == 10


def test_coverage_units(db):
    with db.begin() as s:
        a = make_article(s, units_per_print=1)
        make_dispatch(s, a.id, status="printing", units=2)
        a.units_per_print = 3
        make_entry(s, a.id, 2)
        c = workqueue.coverage_of(s, a)
        assert c.queued_units == 6
        assert c.in_progress_units == 2


def test_held_entries_count(db):
    with db.begin() as s:
        a = make_article(s)
        make_entry(s, a.id, 2, held=True)
        assert workqueue.coverage_of(s, a).queued_units == 2
        assert workqueue.coverage_all(s)[a.id].queued_units == 2


def test_coverage_all_skips_archived(db):
    from reprintbuddy.models import utcnow

    with db.begin() as s:
        a = make_article(s, name="Template A", stock=1)
        b = make_article(s, name="Template B", archived_at=utcnow())
        result = workqueue.coverage_all(s)
        assert set(result) == {a.id}
        assert result[a.id] == workqueue.coverage_of(s, a)


def test_rule_spec_example(db):
    with db.begin() as s:
        automation(s)
        a = make_article(s, auto_enabled=True, min_stock=4, buffer=2, stock=3)
        created = workqueue.apply_rule(s)
        assert len(created) == 1
        rows = entries(s)
        assert len(rows) == 1
        assert (rows[0].article_id, rows[0].prints, rows[0].origin) == (a.id, 3, "rule")
        assert workqueue.apply_rule(s) == []
        assert len(entries(s)) == 1


def test_rule_triggered_by_order(db):
    with db.begin() as s:
        automation(s)
        a = make_article(s, auto_enabled=True, min_stock=1, stock=1).id
        assert workqueue.apply_rule(s) == []
    with db.begin() as s:
        orders.create_order(
            s,
            reference="Order 1001",
            order_date=date(2026, 1, 15),
            items=[ArticleItem(a, 1)],
            actor="user",
        )
        workqueue.apply_rule(s)
        rows = entries(s)
        assert [(r.article_id, r.prints) for r in rows] == [(a, 1)]


@pytest.mark.parametrize(
    "case", ["setting_off", "not_auto", "paused", "archived", "no_minimum"]
)
def test_rule_switches(db, case):
    from reprintbuddy.models import utcnow

    with db.begin() as s:
        automation(s, case != "setting_off")
        fields = dict(auto_enabled=True, min_stock=4, buffer=2, stock=0)
        if case == "not_auto":
            fields["auto_enabled"] = False
        if case == "paused":
            fields["paused"] = True
        if case == "archived":
            fields["archived_at"] = utcnow()
        if case == "no_minimum":
            fields.update(min_stock=0, buffer=0)
        make_article(s, **fields)
        assert workqueue.apply_rule(s) == []
        assert entries(s) == []


def test_rule_journal_auto(db):
    with db.begin() as s:
        automation(s)
        make_article(s, auto_enabled=True, min_stock=4, buffer=2, stock=3)
        workqueue.apply_rule(s)
        row = s.scalars(
            select(JournalEntry).where(JournalEntry.kind == "queue_entry_created")
        ).one()
        assert row.actor == "auto"
        assert row.details["coverage"] == 3
        assert row.details["target"] == 6


def test_rule_after_manual_entry(db):
    with db.begin() as s:
        automation(s)
        a = make_article(s, name="Template A", auto_enabled=True, min_stock=4, stock=0)
        b = make_article(s, name="Template B")
        manual = make_entry(s, b.id, 1, origin="manual", position=0).id
        workqueue.apply_rule(s)
        rows = entries(s)
        assert [r.id for r in rows][0] == manual
        assert rows[1].article_id == a.id


def test_replacement_at_start_even_when_automation_off(db):
    with db.begin() as s:
        a = make_article(s, name="Template A")
        b = make_article(s, name="Template B")
        make_entry(s, b.id, 1, position=0)
        workqueue.add_replacement(s, a.id, 2, "scrap", "user")
        rows = entries(s)
        assert rows[0].article_id == a.id and rows[0].origin == "scrap"


def test_counts_for_pages(db):
    with db.begin() as s:
        a = make_article(s, name="Template A")
        b = make_article(s, name="Template B")
        make_entry(s, a.id, 2)
        make_entry(s, a.id, 3, held=True)
        make_entry(s, b.id, 1)
        make_dispatch(s, a.id, status="queued", units=2)
        make_dispatch(s, a.id, status="printing", units=3)
        make_dispatch(s, a.id, status="completed", units=9)
        assert workqueue.queued_prints(s) == {a.id: 5, b.id: 1}
        assert workqueue.in_progress_prints(s) == {a.id: 2}
        assert workqueue.in_progress_units(s) == {a.id: 5}
