"""Demand from assembly and rule 7.1 for the parts of articles made of parts (Spec 7.1, 0.4.0 p341)."""

from sqlalchemy import select

from factories import make_article, make_assembled
from reprintbuddy import settings as st
from reprintbuddy.domain import assembled, workqueue
from reprintbuddy.models import Article, WorkQueueEntry, utcnow


def automation(s, on=True):
    st.set_setting(s, "automation", on, commit=False)


def queued(s) -> dict[int, int]:
    return {e.article_id: e.prints for e in s.scalars(select(WorkQueueEntry))}


def clip_world(s, *, box_available=2, lamp_available=1):
    """Spec 7.1 example: Clip (4 per print, minimum 2, buffer 2, coverage 3) is twice in the box
    (minimum 3, buffer 2) and once in the lamp (minimum 1)."""
    clip = make_article(s, name="Clip", units_per_print=4, min_stock=2, buffer=2, stock=3,
                        auto_enabled=True, target_model="A1")
    lid = make_article(s, name="Lid")
    box = make_assembled(s, name="Storage box", base=[(clip.id, 2), (lid.id, 1)], min_stock=3, buffer=2,
                         stock=box_available)
    lamp = make_assembled(s, name="Lamp", base=[(clip.id, 1), (lid.id, 1)], min_stock=1, stock=lamp_available)
    return clip, lid, box, lamp


def test_missing_of_counts_only_below_minimum():
    holder = Article(name="x", min_stock=3, buffer=2, stock=2, reserved=0)
    assert assembled.missing_of(holder) == 3
    assert assembled.missing_of(Article(name="x", min_stock=3, buffer=2, stock=4, reserved=1)) == 0  # at minimum
    assert assembled.missing_of(Article(name="x", min_stock=3, buffer=2, stock=5, reserved=4)) == 4  # reserved counts


def test_clip_example_from_the_spec(db):  # Spec 7.1
    with db.begin() as s:
        automation(s)
        clip, lid, _box, _lamp = clip_world(s)
        assert assembled.assembly_demand(s) == {clip.id: 6, lid.id: 3}
        workqueue.apply_rule(s)
        assert queued(s)[clip.id] == 2  # ceil((4 + 6 - 3) / 4)


def test_no_demand_at_or_above_minimum(db):
    with db.begin() as s:
        clip, _lid, _box, _lamp = clip_world(s, box_available=3)
        assert assembled.assembly_demand(s).get(clip.id, 0) == 0


def test_demand_sums_over_variants_and_articles(db):  # review focus 2
    with db.begin() as s:
        segment = make_article(s, name="Segment")
        side = make_article(s, name="Side part L")
        socket32 = make_article(s, name="Socket L32")
        socket40 = make_article(s, name="Socket L40")
        hose = make_assembled(s, name="Hose", sku="EX-H1", base=[(segment.id, 12)], fields=[[
            ("-L32", [(side.id, 1), (socket32.id, 1)]), ("-L40", [(side.id, 1), (socket40.id, 1)]),
        ]])
        make_assembled(s, name="Bracket", base=[(side.id, 2)], min_stock=1)  # missing 1
        rows = {r.sku: r for r in assembled.variants(s, hose)}
        rows["EX-H1-L32"].min_stock, rows["EX-H1-L32"].buffer = 2, 1  # missing 3
        rows["EX-H1-L40"].min_stock = 1  # missing 1
        s.flush()
        demand = assembled.assembly_demand(s)
        assert demand[side.id] == 3 + 1 + 2  # both variants and the second article
        assert demand[segment.id] == 12 * 4
        assert (demand[socket32.id], demand[socket40.id]) == (3, 1)


def test_order_mode_creates_no_demand(db):
    with db.begin() as s:
        segment = make_article(s, name="Segment")
        make_assembled(s, name="Hose", mode="order", base=[(segment.id, 12)], min_stock=0)
        assert assembled.assembly_demand(s) == {}


def test_assembled_articles_are_never_queued(db):
    with db.begin() as s:
        automation(s)
        clip, lid, box, lamp = clip_world(s)
        workqueue.apply_rule(s)
        assert set(queued(s)) <= {clip.id, lid.id}


def test_part_without_own_minimum_prints_for_demand(db):
    with db.begin() as s:
        automation(s)
        panel = make_article(s, name="Panel A", auto_enabled=True, target_model="A1")  # minimum 0, buffer 0
        lid = make_article(s, name="Lid")
        make_assembled(s, name="Storage box", base=[(panel.id, 1), (lid.id, 1)], min_stock=2, buffer=1)
        assert assembled.assembly_demand(s)[panel.id] == 3
        workqueue.apply_rule(s)
        assert queued(s) == {panel.id: 3}


def test_archived_assembled_creates_no_demand(db):
    with db.begin() as s:
        clip, _lid, box, lamp = clip_world(s)
        box.archived_at = lamp.archived_at = utcnow()
        s.flush()
        assert assembled.assembly_demand(s) == {}
