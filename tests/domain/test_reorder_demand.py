from factories import make_article, make_dispatch, make_entry, make_part, make_sort
from reprintbuddy.domain import reorder
from reprintbuddy.models import ArticleFilament, ArticlePart, utcnow


def slot(s, article, sort_id, grams=42.5, n=1):
    s.add(ArticleFilament(article_id=article.id, slot=n, grams=grams, spool_filament_id=sort_id))
    s.flush()


def test_filament_demand_queue_work_and_target(db):
    with db.begin() as s:
        sort = make_sort(s)
        a = make_article(s, min_stock=3, buffer=1, stock=0)
        slot(s, a, sort.id)
        make_entry(s, a.id, 1)
        make_dispatch(s, a.id, status="printing")
        assert reorder.filament_demand(s) == {sort.id: 170.0}


def test_filament_demand_units_per_print(db):
    with db.begin() as s:
        sort = make_sort(s)
        a = make_article(s, units_per_print=2, min_stock=5, buffer=0, stock=0)
        slot(s, a, sort.id)
        assert reorder.filament_demand(s) == {sort.id: 42.5 * 3}


def test_unmapped_slots_not_counted(db):
    with db.begin() as s:
        sort = make_sort(s)
        a = make_article(s, min_stock=1, stock=0)
        slot(s, a, sort.id, n=1)
        slot(s, a, None, grams=10.0, n=2)
        assert reorder.filament_demand(s) == {sort.id: 42.5}


def test_auto_off_still_counts(db):
    with db.begin() as s:
        sort = make_sort(s)
        a = make_article(s, min_stock=2, stock=0, auto_enabled=False)
        slot(s, a, sort.id)
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
        slot(s, a, sort.id)
        s.add(ArticlePart(article_id=a.id, part_id=part.id, qty_per_unit=1))
        make_entry(s, a.id, 2)
        assert reorder.filament_demand(s) == {}
        assert reorder.part_demand(s) == {}
