from datetime import UTC, date, datetime, timedelta

import pytest

from factories import make_article, make_dispatch, make_entry, make_filament_settings, make_part, make_sort
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.domain import filament, reorder, stock
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import ArticleFilament, ArticlePart, JournalEntry, ReorderMark, utcnow

TODAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 6, 12, 0)


def part_with_demand(s, **fields):
    """Insert M4 (pack 50) needed twice per unit by Template A: target 3 gives a demand of 6."""
    values = dict(
        stock=10, pack_size=50, min_order_packs=1, lead_time_days=0, safety_value=20,
        safety_unit="qty", supplier="Example Supplier", order_url="https://shop.example/insert-m4",
    )
    values.update(fields)
    part = make_part(s, **values)
    a = make_article(s, needs_assembly=True, stock=0, min_stock=3, buffer=0)
    s.add(ArticlePart(article_id=a.id, part_id=part.id, qty_per_unit=2))
    s.flush()
    return part


def journal(s, kind, part_id, qty, at, **details):
    s.add(JournalEntry(at=at, actor="auto", kind=kind, part_id=part_id, qty=qty, details=details))
    s.flush()


def filament_setup(s, **settings):
    sort = make_sort(s)
    values = dict(pack_grams=1000, min_order_packs=1, safety_value=2000, safety_unit="grams")
    values.update(settings)
    make_filament_settings(s, sort.id, **values)
    return sort


def with_demand(s, sort):
    """Demand of 170 g: 42.5 g x (1 queued + 1 printing + 2 up to the target)."""
    a = make_article(s, min_stock=3, buffer=1, stock=0)
    s.add(ArticleFilament(article_id=a.id, slot=1, grams=42.5, spool_filament_id=sort.id))
    make_entry(s, a.id, 1)
    make_dispatch(s, a.id, status="printing")
    s.flush()


def sorts_of(sort, grams):
    return {sort.id: filament.SortStock(sort.id, sort.label, "PLA", "#000000", grams, 1)}


def test_part_suggestion_spec_example(db):
    with db.begin() as s:
        part = part_with_demand(s)
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert (sug.item_type, sug.item_id, sug.label) == ("part", part.id, "Insert M4")
        assert sug.packs == 1 and sug.pack_size == 50 and sug.demand == 6
        assert sug.supplier == "Example Supplier"
        assert sug.order_url == "https://shop.example/insert-m4"


def test_part_no_suggestion_when_enough(db):
    with db.begin() as s:
        part_with_demand(s, stock=100)
        assert reorder.suggestions(s, sorts=None, today=TODAY) == []


def test_part_consumption_from_assembly(db):
    with db.begin() as s:
        part = make_part(s)
        old = datetime.combine(TODAY - timedelta(days=10), datetime.min.time())
        journal(s, "inventory", None, 0, old)
        journal(s, "part_consumed", part.id, 30, old + timedelta(days=5), cause="assembly")
        journal(s, "part_consumed", part.id, 20, old + timedelta(days=6), cause="inspection_good")
        assert reorder.part_consumption(s, part.id, TODAY) == 3.0


def test_part_consumption_empty_journal(db):
    with db.begin() as s:
        part = make_part(s)
        assert reorder.part_consumption(s, part.id, TODAY) == 0.0


def test_filament_suggestion(db):
    with db.begin() as s:
        sort = filament_setup(s)
        with_demand(s, sort)
        (sug,) = reorder.suggestions(s, sorts=sorts_of(sort, 1100.0), today=TODAY)
        assert (sug.item_type, sug.item_id, sug.unit) == ("filament", sort.id, "g")
        assert sug.packs == 2 and sug.demand == 170.0 and sug.stock == 1100.0
        assert sug.label == sort.label


def test_sort_without_settings_no_suggestion(db):
    with db.begin() as s:
        sort = make_sort(s)
        with_demand(s, sort)
        assert reorder.suggestions(s, sorts=sorts_of(sort, 0.0), today=TODAY) == []


def test_no_filament_suggestion_without_stock(db):
    with db.begin() as s:
        sort = filament_setup(s)
        with_demand(s, sort)
        part = part_with_demand(s)
        result = reorder.suggestions(s, sorts=None, today=TODAY)
        assert [(x.item_type, x.item_id) for x in result] == [("part", part.id)]


def test_archived_part_no_suggestion(db):
    with db.begin() as s:
        part = part_with_demand(s)
        part.archived_at = utcnow()
        s.flush()
        assert reorder.suggestions(s, sorts=None, today=TODAY) == []


def test_part_under_min_stock_suggests_without_demand(db):  # Spec 9.3: floor = max(safety, min stock)
    with db.begin() as s:
        part = make_part(
            s, stock=18, min_stock=20, pack_size=50, min_order_packs=1, safety_value=0,
            safety_unit="qty",
        )
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert (sug.item_id, sug.demand, sug.packs, sug.pack_size) == (part.id, 0, 1, 50)
        assert sug.safety == 20  # the floor the suggestion was computed with


def test_part_at_min_stock_no_suggestion(db):
    with db.begin() as s:
        make_part(s, stock=20, min_stock=20, pack_size=50, safety_value=0, safety_unit="qty")
        assert reorder.suggestions(s, sorts=None, today=TODAY) == []


def test_part_floor_is_the_larger_of_safety_and_min_stock(db):
    with db.begin() as s:
        # safety 30 beats min stock 20: stock 25 is below 30, 5 are missing -> one pack
        make_part(s, name="A", stock=25, min_stock=20, pack_size=50, safety_value=30, safety_unit="qty")
        # min stock 40 beats safety 5: stock 35 is below 40
        make_part(s, name="B", stock=35, min_stock=40, pack_size=50, safety_value=5, safety_unit="qty")
        # both met: no suggestion (the floors do not add up)
        make_part(s, name="C", stock=30, min_stock=20, pack_size=50, safety_value=10, safety_unit="qty")
        got = reorder.suggestions(s, sorts=None, today=TODAY)
        assert [(x.label, x.safety) for x in got] == [("A", 30), ("B", 40)]


def test_part_min_stock_zero_changes_nothing(db):
    with db.begin() as s:
        make_part(s, stock=0, min_stock=0, pack_size=50, safety_value=0, safety_unit="qty")
        assert reorder.suggestions(s, sorts=None, today=TODAY) == []


def test_part_min_stock_with_demand_counts_both(db):
    with db.begin() as s:
        # stock 10, demand 6, floor 20: 20 + 6 - 10 = 16 missing -> one pack of 50
        part_with_demand(s, stock=10, safety_value=0, min_stock=20)
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert sug.demand == 6 and sug.packs == 1


def test_count_suggestions_counts_part_under_min_stock(db):
    with db.begin() as s:
        make_part(s, stock=18, min_stock=20, pack_size=50, safety_value=0, safety_unit="qty")
        make_part(s, name="Fine", stock=50, min_stock=20, pack_size=50)
        assert reorder.count_suggestions(s, TODAY) == 1


def test_count_suggestions_without_record(db):
    with db.begin() as s:
        sort = filament_setup(s)
        with_demand(s, sort)
        part_with_demand(s)
        assert reorder.count_suggestions(s, TODAY) == 1


def test_open_mark_hides_suggestion(db):
    with db.begin() as s:
        part = part_with_demand(s)
        mark = reorder.mark_ordered(s, "part", part.id, 1)
        assert mark.qty == 1 and mark.received_at is None
        assert reorder.suggestions(s, sorts=None, today=TODAY) == []
        (row,) = reorder.open_marks(s)
        assert (row.item_type, row.item_id, row.label, row.packs) == ("part", part.id, "Insert M4", 1)
        assert (row.pack_size, row.unit) == (50, part.unit)
        reorder.mark_received(s, mark.id)
        assert len(reorder.suggestions(s, sorts=None, today=TODAY)) == 1
        assert reorder.open_marks(s) == []


def test_mark_twice_conflict(db):
    with db.begin() as s:
        part = part_with_demand(s)
        reorder.mark_ordered(s, "part", part.id, 1)
        with pytest.raises(ConflictError) as err:
            reorder.mark_ordered(s, "part", part.id, 1)
        assert err.value.key == "reorder.already_marked"


def test_mark_ordered_validation(db):
    with db.begin() as s:
        part = part_with_demand(s)
        sort = make_sort(s)
        archived = make_part(s, name="Hex key 2", archived_at=utcnow())
        for packs in (0, 1001, "2", 1.5, True):
            with pytest.raises(ValidationError) as err:
                reorder.mark_ordered(s, "part", part.id, packs)
            assert err.value.key == "reorder.invalid_packs"
        for kind, item in (
            ("part", 999), ("part", archived.id), ("filament", sort.id),
            ("filament", 999), ("other", part.id),
        ):
            with pytest.raises(NotFoundError) as err:
                reorder.mark_ordered(s, kind, item, 1)
            assert err.value.key == "reorder.item_not_found"
        assert s.query(ReorderMark).count() == 0


def test_part_mark_closed_by_receipt(db):
    with db.begin() as s:
        part = part_with_demand(s)
        mark = reorder.mark_ordered(s, "part", part.id, 1)
        mark.marked_at -= timedelta(seconds=1)  # the clock may not advance between the two
        s.flush()
        assert reorder.close_arrived_marks(s, None, NOW) == 0
        stock.receive_part(s, part.id, 50, "user")
        assert reorder.close_arrived_marks(s, None, NOW) == 1
        assert mark.received_at is not None
        assert reorder.open_marks(s) == []


def test_receipt_before_mark_does_not_close(db):
    with db.begin() as s:
        part = part_with_demand(s)
        stock.receive_part(s, part.id, 50, "user")
        s.query(JournalEntry).update({"at": utcnow() - timedelta(days=1)})
        reorder.mark_ordered(s, "part", part.id, 1)
        assert reorder.close_arrived_marks(s, None, NOW) == 0


def test_filament_mark_closed_by_new_spools(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s)
        mark = reorder.mark_ordered(s, "filament", sort.id, 2)
        marked = mark.marked_at
        before = fake.add_spool("PLA", created_at=marked - timedelta(days=3))
        no_date = fake.add_spool("PLA", created_at=None)
        other_sort = fake.add_spool("PETG", created_at=marked + timedelta(hours=1))
        first = fake.add_spool("PLA", created_at=marked + timedelta(hours=1))
        spools = [before, no_date, other_sort, first]
        assert reorder.close_arrived_marks(s, spools, NOW) == 0
        assert mark.received_at is None
        second = fake.add_spool("PLA", created_at=marked + timedelta(hours=2))
        assert reorder.close_arrived_marks(s, [*spools, second], NOW) == 1
        assert mark.received_at == NOW


def test_filament_mark_ignores_archived_and_unknown_stock(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s)
        mark = reorder.mark_ordered(s, "filament", sort.id, 1)
        archived = fake.add_spool("PLA", created_at=mark.marked_at + timedelta(hours=1), archived=True)
        assert reorder.close_arrived_marks(s, [archived], NOW) == 0
        assert reorder.close_arrived_marks(s, None, NOW) == 0
        assert mark.received_at is None


def test_filament_mark_aware_created_at(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s)
        mark = reorder.mark_ordered(s, "filament", sort.id, 1)
        aware = (mark.marked_at + timedelta(hours=1)).replace(tzinfo=UTC)
        spool = fake.add_spool("PLA", created_at=aware)
        assert reorder.close_arrived_marks(s, [spool], NOW) == 1


def test_mark_received_manually(db):
    with db.begin() as s:
        part = part_with_demand(s)
        mark = reorder.mark_ordered(s, "part", part.id, 1)
        reorder.mark_received(s, mark.id)
        assert mark.received_at is not None


def test_mark_received_twice(db):
    with db.begin() as s:
        part = part_with_demand(s)
        mark = reorder.mark_ordered(s, "part", part.id, 1)
        reorder.mark_received(s, mark.id)
        with pytest.raises(ConflictError) as err:
            reorder.mark_received(s, mark.id)
        assert err.value.key == "reorder.already_received"
        with pytest.raises(NotFoundError) as err:
            reorder.mark_received(s, 999)
        assert err.value.key == "reorder.mark_not_found"
