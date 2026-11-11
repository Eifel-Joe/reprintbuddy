"""A cancel takes back the reprints planned for the demand it removes (Spec 7.2a, 0.4.1 p382).

The surplus is counted per affected print article: coverage minus the target (with demand from
assembly). Rule entries in Reprintbuddy's queue shrink at once, from the bottom; what is left over is
noted for Bambuddy (waiting rule prints there, newest first). Nothing pauses; the journal says it.
"""

from datetime import date

import pytest
from sqlalchemy import select

from factories import make_article, make_assembled, make_dispatch, make_entry, make_set
from reprintbuddy import settings as st
from reprintbuddy.domain import assembled, orders, withdraw
from reprintbuddy.domain.orders import ArticleItem, AssembledItem, SetItem
from reprintbuddy.models import Article, JournalEntry, Order, Withdrawal, WorkQueueEntry, utcnow

DAY = date(2026, 11, 11)


def order(s, *items, reference="R-1"):
    return orders.create_order(s, reference=reference, order_date=DAY, items=list(items), actor="user").id


def queue(s):
    rows = s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position))
    return [(r.article_id, r.origin, r.prints, r.held) for r in rows]


def noted(s):
    return {w.article_id: (w.prints, w.order_id) for w in s.scalars(select(Withdrawal))}


def kinds(s):
    return [j.kind for j in s.scalars(select(JournalEntry).order_by(JournalEntry.id))]


def template(s, **fields):
    """Minimum 2, buffer 1 (target 3), stock 3, one piece per print: the examples of the proposal."""
    values = dict(min_stock=2, buffer=1, stock=3, auto_enabled=True, target_model="A1")
    values.update(fields)
    return make_article(s, **values)


# ---- question 1: how many --------------------------------------------------------------------


def test_everything_planned_for_the_order_comes_back(db):
    with db.begin() as s:
        a = template(s).id
        order_id = order(s, ArticleItem(a, 5))  # available -2: five prints planned
        make_entry(s, a, 5)
        report = orders.cancel_order(s, order_id, "user")
        assert report == withdraw.WithdrawReport(queue_prints=5, bambuddy_prints=0)
        assert queue(s) == []
        assert s.get(Order, order_id).status == "cancelled"


def test_only_the_surplus_over_the_target_comes_back(db):
    with db.begin() as s:
        a = template(s).id
        keep = order(s, ArticleItem(a, 2), reference="R-1")
        gone = order(s, ArticleItem(a, 3), reference="R-2")
        make_entry(s, a, 5)
        report = orders.cancel_order(s, gone, "user")
        assert report.queue_prints == 3
        assert queue(s) == [(a, "rule", 2, False)]  # two stay for the other order
        assert keep


@pytest.mark.parametrize(("stock", "back"), [(4, 2), (1, 1)])
def test_whole_prints_only_never_below_the_target(db, stock, back):
    """4 pieces per print, target 4, two prints (8 pieces) planned. Stock 4: 8 over the target, both
    back. Stock 1: 5 over, one back (both would leave 1 + 0 = 1, below the target)."""
    with db.begin() as s:
        a = template(s, units_per_print=4, min_stock=2, buffer=2, stock=stock).id
        order_id = order(s, ArticleItem(a, 6))
        make_entry(s, a, 2)
        assert orders.cancel_order(s, order_id, "user").queue_prints == back


def test_target_without_a_minimum_is_the_buffer(db):
    with db.begin() as s:
        a = template(s, min_stock=0, buffer=0, stock=0).id
        make_entry(s, a, 2)  # left over from a minimum that was lowered later
        order_id = order(s, ArticleItem(a, 1))
        assert orders.cancel_order(s, order_id, "user").queue_prints == 2


# ---- question 2: order, held entries, Bambuddy --------------------------------------------------


def test_from_the_bottom_held_entries_included(db):
    with db.begin() as s:
        a = template(s, stock=2).id  # after the cancel: 2 + 4 planned = 6, three over the target
        b = template(s, name="Template B").id
        order_id = order(s, ArticleItem(a, 3))
        first = make_entry(s, a, 2).id
        make_entry(s, b, 1)
        make_entry(s, a, 2, held=True)
        orders.cancel_order(s, order_id, "user")  # surplus 3: the held two, then one of the first
        assert queue(s) == [(a, "rule", 1, False), (b, "rule", 1, False)]
        assert s.get(WorkQueueEntry, first).position == 0
        assert [e.position for e in s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position))] == [0, 1]


def test_the_rest_is_noted_for_waiting_prints_in_bambuddy(db):
    with db.begin() as s:
        a = template(s).id
        order_id = order(s, ArticleItem(a, 5))
        make_entry(s, a, 1)
        for status in ("queued", "queued", "queued", "printing"):
            make_dispatch(s, a, status=status, bambuddy_item_id=1)
        report = orders.cancel_order(s, order_id, "user")
        # surplus 5: one from the queue, three waiting in Bambuddy; the printing one stays
        assert report == withdraw.WithdrawReport(queue_prints=1, bambuddy_prints=3)
        assert noted(s) == {a: (3, order_id)}


def test_prints_on_their_way_are_never_candidates(db):  # review focus 5
    with db.begin() as s:
        a = template(s).id
        order_id = order(s, ArticleItem(a, 3))
        make_dispatch(s, a, status="sending")
        make_dispatch(s, a, status="unclear", enqueue_sent_at=utcnow())
        make_dispatch(s, a, status="queued", bambuddy_item_id=1, withdraw_sent_at=utcnow())
        report = orders.cancel_order(s, order_id, "user")
        assert report == withdraw.WithdrawReport(0, 0)
        assert noted(s) == {}


def test_newest_waiting_prints_first(db):
    with db.begin() as s:
        a = template(s).id
        old = make_dispatch(s, a, bambuddy_item_id=1, dispatched_at=utcnow()).id
        new = make_dispatch(s, a, bambuddy_item_id=2, dispatched_at=utcnow()).id
        assert [d.id for d in withdraw.bambuddy_candidates(s, a)] == [new, old]


# ---- question 3: origin ------------------------------------------------------------------------


def test_manual_and_replacement_entries_stay(db):
    with db.begin() as s:
        a = template(s).id
        order_id = order(s, ArticleItem(a, 2))
        make_entry(s, a, 2, origin="manual")
        make_entry(s, a, 1, origin="scrap")
        make_entry(s, a, 2)
        make_dispatch(s, a, origin="manual", bambuddy_item_id=1)
        make_dispatch(s, a, origin="failure", bambuddy_item_id=2)
        report = orders.cancel_order(s, order_id, "user")
        assert report == withdraw.WithdrawReport(queue_prints=2, bambuddy_prints=0)
        assert queue(s) == [(a, "manual", 2, False), (a, "scrap", 1, False)]


# ---- question 5: automation off, paused ------------------------------------------------------


@pytest.mark.parametrize("case", ["global_off", "article_off", "paused"])
def test_also_without_automation_and_when_paused(db, case):
    with db.begin() as s:
        st.set_setting(s, "automation", case != "global_off", commit=False)
        a = template(s, auto_enabled=case != "article_off", paused=case == "paused",
                     pause_reason="queue_reduced" if case == "paused" else None).id
        order_id = order(s, ArticleItem(a, 2))
        make_entry(s, a, 2)
        assert orders.cancel_order(s, order_id, "user").queue_prints == 2
        art = s.get(Article, a)
        assert (art.paused, art.pause_reason) == ((True, "queue_reduced") if case == "paused" else (False, None))


# ---- nothing pauses, the journal says it ---------------------------------------------------------


def test_no_pause_and_one_journal_line_per_article(db):
    with db.begin() as s:
        a = template(s).id
        order_id = order(s, ArticleItem(a, 3))
        make_entry(s, a, 1)
        make_entry(s, a, 2)
        make_dispatch(s, a, bambuddy_item_id=1)
        orders.cancel_order(s, order_id, "user")  # 3 + 3 + 1 = 7: four over the target
        assert not s.get(Article, a).paused
        assert kinds(s) == ["order_created", "order_cancelled", "queue_entry_withdrawn"]
        line = s.scalars(select(JournalEntry).where(JournalEntry.kind == "queue_entry_withdrawn")).one()
        assert (line.actor, line.qty, line.article_id, line.order_id) == ("auto", 3, a, order_id)
        assert noted(s) == {a: (1, order_id)}  # the fourth: the print waiting in Bambuddy


def test_a_second_cancel_replaces_the_note(db):  # review focus 4
    with db.begin() as s:
        a = template(s).id
        first = order(s, ArticleItem(a, 2), reference="R-1")
        second = order(s, ArticleItem(a, 2), reference="R-2")
        for item in range(4):
            make_dispatch(s, a, bambuddy_item_id=item + 1)
        orders.cancel_order(s, first, "user")
        assert noted(s) == {a: (2, first)}
        orders.cancel_order(s, second, "user")
        assert noted(s) == {a: (4, second)}  # the surplus now, not 2 + 4


def test_no_surplus_drops_an_old_note(db):
    with db.begin() as s:
        a = template(s).id
        first = order(s, ArticleItem(a, 2), reference="R-1")
        make_dispatch(s, a, bambuddy_item_id=1)
        make_dispatch(s, a, bambuddy_item_id=2)
        orders.cancel_order(s, first, "user")
        assert noted(s) == {a: (2, first)}
        s.get(Article, a).stock = 0  # meanwhile sold from the shelf: the prints are needed again
        second = order(s, ArticleItem(a, 1), reference="R-2")
        orders.cancel_order(s, second, "user")
        assert noted(s) == {}


# ---- affected articles: sets and articles made of parts (A4) ----------------------------------


def test_a_set_order_takes_back_base_and_chosen_parts(db):
    with db.begin() as s:
        a = template(s, name="Template A").id
        b = template(s, name="Template B").id
        c = template(s, name="Template C").id
        set_id = make_set(s, "Starter Set", [(1, a)], [(2, [(b, "-B"), (c, "-C")])], sku="S-1").id
        order_id = order(s, SetItem(set_id, 2, (b,)))  # A x 2, B x 4
        assert withdraw.affected_articles(s, order_id) == [a, b]
        make_entry(s, a, 2)
        make_entry(s, b, 4)
        make_entry(s, c, 1)  # not in the order: untouched although it is over its target too
        assert orders.cancel_order(s, order_id, "user").queue_prints == 6
        assert queue(s) == [(c, "rule", 1, False)]


def test_parts_of_an_article_built_on_order(db):
    with db.begin() as s:
        segment = template(s, name="Segment", min_stock=0, buffer=0, stock=0).id
        composite = make_assembled(s, name="Hose", sku="EX-H1", mode="order", base=[(segment, 3)])
        key = assembled.variants(s, composite)[0].variant_key or ""
        order_id = order(s, AssembledItem(composite.id, 2, key))
        assert withdraw.affected_articles(s, order_id) == [segment]
        make_entry(s, segment, 6)
        assert orders.cancel_order(s, order_id, "user").queue_prints == 6


def test_parts_of_a_stock_variant_through_the_demand(db):
    with db.begin() as s:
        clip = template(s, name="Clip", min_stock=0, buffer=0, stock=0).id
        box = make_assembled(s, name="Storage box", sku="EX-B1", base=[(clip, 2)], min_stock=1, stock=1)
        order_id = order(s, ArticleItem(box.id, 2))  # box available -1: missing 2, clip demand 4
        assert withdraw.affected_articles(s, order_id) == [clip]
        make_entry(s, clip, 4)
        assert orders.cancel_order(s, order_id, "user").queue_prints == 4  # demand 0 again
        assert queue(s) == []


def test_archived_articles_are_left_alone(db):
    with db.begin() as s:
        a = template(s).id
        order_id = order(s, ArticleItem(a, 2))
        make_entry(s, a, 2)
        s.get(Article, a).archived_at = utcnow()  # cannot happen through the interface (Spec 5.5)
        assert withdraw.affected_articles(s, order_id) == []


# ---- the preview on the confirmation page (A3) ---------------------------------------------------


def test_preview_changes_nothing(db):
    with db.begin() as s:
        a = template(s).id
        order_id = order(s, ArticleItem(a, 5))
        make_entry(s, a, 2)
        for item in range(3):
            make_dispatch(s, a, bambuddy_item_id=item + 1)
    with db.begin() as s:
        before = (queue(s), kinds(s), s.get(Article, a).reserved)
    # Its own session, rolled back and never committed: nothing is published or reported (A3).
    assert withdraw.preview_cancel(db, order_id) == withdraw.WithdrawReport(2, 3)
    with db.begin() as s:
        assert (queue(s), kinds(s), s.get(Article, a).reserved) == before
        assert s.get(Order, order_id).status == "open" and noted(s) == {}


def test_preview_of_a_closed_order_is_nothing(db):
    with db.begin() as s:
        a = template(s).id
        order_id = order(s, ArticleItem(a, 1))
        orders.cancel_order(s, order_id, "user")
    assert withdraw.preview_cancel(db, order_id) == withdraw.WithdrawReport(0, 0)
    assert withdraw.preview_cancel(db, 999) == withdraw.WithdrawReport(0, 0)
