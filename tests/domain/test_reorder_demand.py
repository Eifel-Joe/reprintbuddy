"""Planned demand per reordered spool sort over its slots, and per purchased part (Spec 9.2)."""

from factories import make_article, make_dispatch, make_entry, make_part, make_sort, tick_sorts
from reprintbuddy.domain import reorder
from reprintbuddy.models import ArticleFilament, ArticlePart, utcnow


def slot(s, article, *sorts, grams=42.5, n=1, reordered=None):
    """A slot of `article` (PLA black) with `sorts` ticked; the first is reordered unless named."""
    s.add(ArticleFilament(article_id=article.id, slot=n, grams=grams, material="PLA", color_hex="#000000"))
    s.flush()
    if sorts:
        tick_sorts(s, article.id, n, *sorts, reordered=reordered)


def test_filament_demand_queue_work_and_target(db):
    with db.begin() as s:
        sort = make_sort(s)
        a = make_article(s, min_stock=3, buffer=1, stock=0)
        slot(s, a, sort)
        make_entry(s, a.id, 1)
        make_dispatch(s, a.id, status="printing")
        assert reorder.filament_demand(s) == {sort.id: 170.0}


def test_filament_demand_units_per_print(db):
    with db.begin() as s:
        sort = make_sort(s)
        a = make_article(s, units_per_print=2, min_stock=5, buffer=0, stock=0)
        slot(s, a, sort)
        assert reorder.filament_demand(s) == {sort.id: 42.5 * 3}


def test_slot_without_group_not_counted(db):
    with db.begin() as s:
        sort = make_sort(s)
        a = make_article(s, min_stock=1, stock=0)
        slot(s, a, sort, n=1)
        slot(s, a, grams=10.0, n=2)
        assert reorder.filament_demand(s) == {sort.id: 42.5}


def test_demand_belongs_to_the_reordered_sort_only(db):
    """A second ticked sort shares the slot's stock, not its demand (Spec 9.2)."""
    with db.begin() as s:
        reordered = make_sort(s)
        other = make_sort(s, brand="Brand B")
        a = make_article(s, min_stock=1, stock=0)
        slot(s, a, other, reordered, reordered=reordered)
        assert reorder.filament_demand(s) == {reordered.id: 42.5}


def test_demand_adds_up_over_the_slots_of_a_sort(db):
    with db.begin() as s:
        a_sort = make_sort(s)
        b_sort = make_sort(s, brand="Brand B")
        one = make_article(s, name="One", min_stock=1, stock=0)
        two = make_article(s, name="Two", min_stock=2, stock=0)
        slot(s, one, a_sort, n=1)
        slot(s, one, b_sort, grams=5.0, n=2)
        slot(s, two, a_sort, b_sort, grams=10.0, reordered=a_sort)
        assert reorder.filament_demand(s) == {a_sort.id: 42.5 + 20.0, b_sort.id: 5.0}


def test_auto_off_still_counts(db):
    with db.begin() as s:
        sort = make_sort(s)
        a = make_article(s, min_stock=2, stock=0, auto_enabled=False)
        slot(s, a, sort)
        assert reorder.filament_demand(s).get(sort.id) == 85.0


def test_part_demand(db):
    with db.begin() as s:
        part = make_part(s)
        a = make_article(
            s, needs_assembly=True, stock=0, awaiting_assembly=1, min_stock=5, buffer=0
        )
        s.add(ArticlePart(article_id=a.id, part_id=part.id, qty_per_unit=2))
        make_entry(s, a.id, 1)
        make_dispatch(s, a.id, status="queued", units=1)
        assert reorder.part_demand(s) == {part.id: 10}


def test_archived_articles_no_demand(db):
    with db.begin() as s:
        sort = make_sort(s)
        part = make_part(s)
        a = make_article(s, min_stock=3, stock=0, archived_at=utcnow())
        slot(s, a, sort)
        s.add(ArticlePart(article_id=a.id, part_id=part.id, qty_per_unit=1))
        make_entry(s, a.id, 2)
        assert reorder.filament_demand(s) == {}
        assert reorder.part_demand(s) == {}
