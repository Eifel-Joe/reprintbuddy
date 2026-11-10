from datetime import UTC, date, datetime, timedelta

import pytest

from factories import (
    assign_sort,
    make_article,
    make_dispatch,
    make_entry,
    make_filament_settings,
    make_part,
    make_snapshot,
    make_sort,
    tick_sorts,
)
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.domain import catalog, filament, reorder, stock
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


def filament_setup(s, sort=None, **settings):
    """Settings for `sort` (default: a new Brand A PLA black): pack 1000 g, safety 2000 g."""
    sort = sort if sort is not None else make_sort(s, brand="Brand A")
    values = dict(pack_grams=1000, min_order_packs=1, safety_value=2000, safety_unit="grams")
    values.update(settings)
    make_filament_settings(s, sort.id, **values)
    return sort


def with_demand(s, sort, *others, name="Template A"):
    """Demand of 170 g on `sort`: 42.5 g x (1 queued + 1 printing + 2 up to the target); `others`
    are ticked in the same slot without being reordered."""
    a = make_article(s, name=name, min_stock=3, buffer=1, stock=0)
    s.add(ArticleFilament(article_id=a.id, slot=1, grams=42.5, material="PLA", color_hex="#000000"))
    s.flush()
    tick_sorts(s, a.id, 1, sort, *others, reordered=sort)
    make_entry(s, a.id, 1)
    make_dispatch(s, a.id, status="printing")
    s.flush()
    return a


def sorts_of(*stock):
    """`sorts_of((sort, grams), ...)`: the stock map `suggestions` takes."""
    return {
        sort.id: filament.SortStock(sort.id, sort.label, sort.material, sort.color_hex, grams, 1)
        for sort, grams in stock
    }


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


def test_part_consumption_from_every_booking_that_takes_parts_off(db):
    """Spec 9.2 (p174): usage = the parts taken off by assembly, by "inspection good" of an article
    without hand work and by stock in without a print with "deduct parts" ticked (Spec 6.6)."""
    with db.begin() as s:
        part = make_part(s)
        other = make_part(s, name="Other part")
        old = datetime.combine(TODAY - timedelta(days=10), datetime.min.time())
        journal(s, "inventory", None, 0, old)
        journal(s, "part_consumed", part.id, 30, old + timedelta(days=5), cause="assembly")
        journal(s, "part_consumed", part.id, 20, old + timedelta(days=6), cause="inspection_good")
        journal(s, "part_consumed", part.id, 10, old + timedelta(days=7), cause="stock_in")
        journal(s, "part_consumed", other.id, 99, old + timedelta(days=7), cause="assembly")  # another part
        journal(s, "part_receipt", part.id, 500, old + timedelta(days=8))  # a receipt is no usage
        assert reorder.part_consumption(s, part.id, TODAY) == 6.0  # 60 over 10 days


def test_parts_of_an_article_without_hand_work_count_from_inspection(db):  # p174, end to end
    with db.begin() as s:
        part = make_part(s, stock=100)
        a = make_article(s, needs_assembly=False, provisional=5)
        s.add(ArticlePart(article_id=a.id, part_id=part.id, qty_per_unit=2))
        s.flush()
        stock.inspect_good(s, a.id, 5, "user")
        stock.stock_in(s, a.id, 1, "user", consume_parts=True)
        stock.stock_in(s, a.id, 1, "user", consume_parts=False)  # unticked: no parts, no usage
        assert reorder.part_consumption(s, part.id, utcnow().date()) == 12.0  # (10 + 2) over 1 day


def test_part_consumption_empty_journal(db):
    with db.begin() as s:
        part = make_part(s)
        assert reorder.part_consumption(s, part.id, TODAY) == 0.0


def test_filament_suggestion(db):
    with db.begin() as s:
        sort = filament_setup(s)
        with_demand(s, sort)
        (sug,) = reorder.suggestions(s, sorts=sorts_of((sort, 1100.0)), today=TODAY)
        assert (sug.item_type, sug.item_id, sug.unit) == ("filament", sort.id, "g")
        assert sug.packs == 2 and sug.demand == 170.0 and sug.stock == 1100.0
        assert sug.label == sort.label == "Brand A PLA Black"


def test_sort_without_settings_no_suggestion(db):
    with db.begin() as s:
        sort = make_sort(s, brand="Brand A")
        with_demand(s, sort)
        assert reorder.suggestions(s, sorts=sorts_of((sort, 0.0)), today=TODAY) == []


def test_no_filament_suggestion_without_stock(db):
    with db.begin() as s:
        sort = filament_setup(s)
        with_demand(s, sort)
        part = part_with_demand(s)
        result = reorder.suggestions(s, sorts=None, today=TODAY)
        assert [(x.item_type, x.item_id) for x in result] == [("part", part.id)]


# ---- stock and usage over the filament group (Spec 9.2) ---------------------------------------


def test_two_brands_of_black_only_the_ticked_one_counts(db):
    with db.begin() as s:
        ticked = filament_setup(s)
        untouched = make_sort(s, brand="Brand B")
        with_demand(s, ticked)
        (sug,) = reorder.suggestions(s, sorts=sorts_of((ticked, 1500.0), (untouched, 5000.0)), today=TODAY)
        assert sug.item_id == ticked.id and sug.stock == 1500.0


def test_stock_is_the_sum_of_every_sort_ticked_in_the_slots(db):
    with db.begin() as s:
        reordered = filament_setup(s)
        second = make_sort(s, brand="Brand B")
        with_demand(s, reordered, second)
        stock_map = sorts_of((reordered, 500.0), (second, 600.0))
        (sug,) = reorder.suggestions(s, sorts=stock_map, today=TODAY)
        assert sug.stock == 1100.0 and sug.packs == 2  # 2000 + 170 - 1100 = 1070 missing
        stock_map = sorts_of((reordered, 500.0), (second, 2000.0))
        assert reorder.suggestions(s, sorts=stock_map, today=TODAY) == []  # 2500 - 170 >= 2000


def test_a_ticked_sort_without_reordered_has_no_suggestion_of_its_own(db):
    with db.begin() as s:
        reordered = filament_setup(s)
        second = filament_setup(s, make_sort(s, brand="Brand B"))  # settings alone do not do it
        with_demand(s, reordered, second)
        result = reorder.suggestions(s, sorts=sorts_of((reordered, 0.0), (second, 0.0)), today=TODAY)
        assert [x.item_id for x in result] == [reordered.id]


def test_a_sort_in_two_groups_counts_in_both(db):
    """Spec 9.2: a sort ticked in the slots of two reordered sorts counts for both (so wanted)."""
    with db.begin() as s:
        a = filament_setup(s)
        b = filament_setup(s, make_sort(s, brand="Brand B"))
        with_demand(s, a, b, name="One")
        with_demand(s, b, name="Two")
        result = reorder.suggestions(s, sorts=sorts_of((a, 300.0), (b, 700.0)), today=TODAY)
        assert sorted((x.item_id, x.stock) for x in result) == [(a.id, 1000.0), (b.id, 700.0)]


def test_slot_without_group_counts_for_nothing(db):
    with db.begin() as s:
        sort = filament_setup(s)
        a = make_article(s, min_stock=3, stock=0)
        s.add(ArticleFilament(article_id=a.id, slot=1, grams=42.5, material="PLA", color_hex="#000000"))
        s.flush()
        assert reorder.filament_demand(s) == {}
        assert reorder.suggestions(s, sorts=sorts_of((sort, 0.0)), today=TODAY) == []


def test_an_archived_article_counts_for_nothing(db):
    with db.begin() as s:
        sort = filament_setup(s)
        assign_sort(s, sort, archived=True)
        assert reorder.suggestions(s, sorts=sorts_of((sort, 0.0)), today=TODAY) == []


def test_usage_over_the_group_and_has_history(db):  # p59
    with db.begin() as s:
        reordered = filament_setup(s, safety_value=0, lead_time_days=10)
        second = make_sort(s, brand="Brand B")
        with_demand(s, reordered, second)
        stock_map = sorts_of((reordered, 50.0), (second, 50.0))  # 100 g for a demand of 170 g
        (sug,) = reorder.suggestions(s, sorts=stock_map, today=TODAY)
        assert (sug.per_day, sug.has_history) == (0.0, False)  # no daily rows yet
        for back in range(3):  # only the second sort is used: 20 g a day
            make_snapshot(s, TODAY - timedelta(days=back), 7, second.id, 100.0 + 20.0 * back)
        (sug,) = reorder.suggestions(s, sorts=stock_map, today=TODAY)
        assert (sug.per_day, sug.has_history) == (20.0, True)


def test_part_has_history_with_any_journal_row(db):  # p59
    with db.begin() as s:
        part_with_demand(s)
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert sug.has_history is False
        journal(s, "inventory", None, 0, NOW - timedelta(days=1))
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert sug.has_history is True


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


def test_part_floor_with_safety_in_days(db):
    with db.begin() as s:
        # 3 per day for 5 days = 15 safety; min stock 20 is the larger floor: stock 19 -> suggestion
        part = make_part(s, stock=19, min_stock=20, pack_size=50, safety_value=5, safety_unit="days")
        old = datetime.combine(TODAY - timedelta(days=10), datetime.min.time())
        journal(s, "inventory", None, 0, old)
        journal(s, "part_consumed", part.id, 30, old + timedelta(days=5), cause="assembly")
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert sug.per_day == 3.0 and sug.safety == 20
        # stock 22 - 0 - 0 = 22 >= 20 and the cushion (15) is smaller: no suggestion
        part.stock = 22
        s.flush()
        assert reorder.suggestions(s, sorts=None, today=TODAY) == []


def test_count_suggestions_counts_part_under_min_stock(db):
    with db.begin() as s:
        make_part(s, stock=18, min_stock=20, pack_size=50, safety_value=0, safety_unit="qty")
        make_part(s, name="Fine", stock=50, min_stock=20, pack_size=50)
        assert reorder.count_suggestions(s, None, NOW) == 1


def test_count_without_writing_uses_the_record_and_writes_nothing(db):
    """While writes are paused (restore, update) the overview counts without writing (v0.3.14 p167)."""
    with db.begin() as s:
        make_part(s, stock=18, min_stock=20, pack_size=50, safety_value=0, safety_unit="qty")
    with db() as s:
        assert reorder.count_suggestions(s, None, NOW, write=False) == 1
        assert not s.new and not s.dirty and not s.deleted


# ---- p64: the key figure is the number the reorder page shows --------------------------------


def _count_and_page(s, spools):
    count = reorder.count_suggestions(s, spools, NOW)
    page = reorder.current(s, spools, NOW)
    assert count == len(page.suggestions)
    return count, page


def test_count_matches_the_page_live(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s)
        with_demand(s, sort)
        part_with_demand(s)
        spools = [fake.add_spool("PLA", brand="Brand A", remaining_g=500.0)]
        count, page = _count_and_page(s, spools)
        assert count == 2 and page.day is None and page.sorts[sort.id].stock_g == 500.0
        assert [x.item_type for x in page.suggestions] == ["filament", "part"]


def test_count_matches_the_page_from_the_record(db):
    with db.begin() as s:
        sort = filament_setup(s)
        with_demand(s, sort)
        part_with_demand(s)
        make_snapshot(s, TODAY - timedelta(days=1), 7, sort.id, 500.0)
        count, page = _count_and_page(s, None)
        assert count == 2 and page.day == TODAY - timedelta(days=1)
        assert page.sorts[sort.id].stock_g == 500.0


def test_count_matches_the_page_when_nothing_is_known(db):
    with db.begin() as s:
        sort = filament_setup(s)
        with_demand(s, sort)
        part = part_with_demand(s)
        count, page = _count_and_page(s, None)
        assert count == 1 and page.sorts is None and page.day is None
        assert [(x.item_type, x.item_id) for x in page.suggestions] == [("part", part.id)]


def test_count_closes_a_received_part_mark_first(db):
    with db.begin() as s:
        part = part_with_demand(s, pack_size=1)  # 1 ordered: one receipt of 1 is the whole order (p211)
        mark = reorder.mark_ordered(s, "part", part.id, 1)
        mark.marked_at -= timedelta(seconds=1)  # the clock may not advance between the two
        s.flush()
        assert reorder.suggestions(s, sorts=None, today=TODAY) == []  # hidden while open
        stock.receive_part(s, part.id, 1, "user")  # stock 11: still short, the suggestion is back
        count, page = _count_and_page(s, None)
        assert count == 1 and mark.received_at is not None
        assert [x.item_id for x in page.suggestions] == [part.id]


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
        before = fake.add_spool("PLA", brand="Brand A", created_at=marked - timedelta(days=3))
        no_date = fake.add_spool("PLA", brand="Brand A", created_at=None)
        other_sort = fake.add_spool("PETG", brand="Brand A", created_at=marked + timedelta(hours=1))
        other_brand = fake.add_spool("PLA", brand="Brand B", created_at=marked + timedelta(hours=1))
        first = fake.add_spool("PLA", brand="Brand A", created_at=marked + timedelta(hours=1))
        spools = [before, no_date, other_sort, other_brand, first]
        filament.sync_sorts(s, spools)
        assert reorder.close_arrived_marks(s, spools, NOW) == 0
        assert mark.received_at is None
        second = fake.add_spool("PLA", brand="Brand A", created_at=marked + timedelta(hours=2))
        assert reorder.close_arrived_marks(s, [*spools, second], NOW) == 1
        assert mark.received_at == NOW


def test_filament_mark_ignores_archived_and_unknown_stock(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s)
        mark = reorder.mark_ordered(s, "filament", sort.id, 1)
        archived = fake.add_spool("PLA", brand="Brand A", created_at=mark.marked_at + timedelta(hours=1), archived=True)
        assert reorder.close_arrived_marks(s, [archived], NOW) == 0
        assert reorder.close_arrived_marks(s, None, NOW) == 0
        assert mark.received_at is None


def test_filament_mark_aware_created_at(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s)
        mark = reorder.mark_ordered(s, "filament", sort.id, 1)
        aware = (mark.marked_at + timedelta(hours=1)).replace(tzinfo=UTC)
        spool = fake.add_spool("PLA", brand="Brand A", created_at=aware)
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


# ---- p60: the mark of an archived part -------------------------------------------------------


def test_mark_of_an_archived_part_waits_for_a_restore(db):
    with db.begin() as s:
        part = make_part(s, pack_size=50)
        mark = reorder.mark_ordered(s, "part", part.id, 1)
        catalog.archive_part(s, part.id)
        assert reorder.open_marks(s) == []
        assert mark.received_at is None  # the mark stays open
        assert reorder.close_arrived_marks(s, None, NOW) == 0
        catalog.restore_part(s, part.id)
        (row,) = reorder.open_marks(s)
        assert (row.id, row.item_id, row.label) == (mark.id, part.id, "Insert M4")


def test_a_legacy_sort_cannot_be_marked_ordered(db):
    """A sort of 0006 waiting for its conversion still has its settings, but it is no spool sort to order."""
    from factories import make_legacy_sort

    with db.begin() as s:
        old = make_legacy_sort(s)
        make_filament_settings(s, old.id)
        with pytest.raises(NotFoundError) as err:
            reorder.mark_ordered(s, "filament", old.id, 1)
        assert err.value.key == "reorder.item_not_found"
        assert s.query(ReorderMark).count() == 0


# ---- p171: an "ordered" mark counts as on its way (Spec 9.4) ----------------------------------


def test_ordered_filament_counts_as_on_its_way_in_other_groups(db):
    """Spec 9.4: the ordered amount counts as on its way, also in the group of another reordered
    sort where the marked sort is ticked (else that suggestion comes too early)."""
    with db.begin() as s:
        a = filament_setup(s)
        b = filament_setup(s, make_sort(s, brand="Brand B"))
        with_demand(s, a, b, name="One")
        with_demand(s, b, name="Two")
        stock_map = sorts_of((a, 300.0), (b, 700.0))
        reorder.mark_ordered(s, "filament", b.id, 1)  # 1000 g on its way
        (sug,) = reorder.suggestions(s, sorts=stock_map, today=TODAY)
        assert (sug.item_id, sug.stock, sug.on_way) == (a.id, 1000.0, 1000.0)
        assert sug.packs == 1  # 2000 + 170 - (1000 + 1000) = 170 missing: one pack, not two
        reorder.mark_received(s, reorder.open_marks(s)[0].id)
        reorder.mark_ordered(s, "filament", b.id, 2)  # 2000 g on its way: 3000 - 170 >= 2000
        assert [x.item_id for x in reorder.suggestions(s, sorts=stock_map, today=TODAY)] == []


def test_ordered_amount_is_on_its_way_only_until_it_arrived(db):
    with db.begin() as s:
        a = filament_setup(s)
        b = filament_setup(s, make_sort(s, brand="Brand B"))
        with_demand(s, a, b, name="One")
        mark = reorder.mark_ordered(s, "filament", b.id, 2)
        reorder.mark_received(s, mark.id)
        (sug,) = reorder.suggestions(s, sorts=sorts_of((a, 300.0), (b, 700.0)), today=TODAY)
        assert (sug.item_id, sug.on_way, sug.packs) == (a.id, 0.0, 2)


def test_on_way_amounts_of_parts_and_filament(db):
    """The amounts that count as on its way, in the item's unit: packs x pack size (Spec 9.4)."""
    with db.begin() as s:
        part = part_with_demand(s)
        sort = filament_setup(s, pack_grams=750)
        reorder.mark_ordered(s, "part", part.id, 3)
        reorder.mark_ordered(s, "filament", sort.id, 2)
        assert reorder.on_way(s) == {("part", part.id): 150.0, ("filament", sort.id): 1500.0}


# ---- p172: usage counts by the booking date, not by when it was saved (Spec 9.2) ----------------


def test_part_consumption_counts_by_the_booking_date(db):
    with db.begin() as s:
        part = make_part(s)
        now = datetime.combine(TODAY, datetime.min.time())
        old = now - timedelta(days=10)
        s.add(JournalEntry(at=now, booked_at=old, actor="user", kind="inventory", qty=0))  # first booking: 10 days ago
        s.add(JournalEntry(at=now, booked_at=old + timedelta(days=1), actor="user", kind="part_consumed",
                           part_id=part.id, qty=30, details={"cause": "assembly"}))
        s.add(JournalEntry(at=now, booked_at=now - timedelta(days=40), actor="user", kind="part_consumed",
                           part_id=part.id, qty=99, details={"cause": "assembly"}))  # outside the window
        s.flush()
        # the oldest row is booked 40 days back: 30 days of history; the 99 outside the window do not count
        assert reorder.part_consumption(s, part.id, TODAY) == 1.0  # 30 over 30 days; by `at` it would be 129 / 1


# ---- several suppliers per part (Spec 9.3, 9.4) -----------------------------------------------


def add_supplier(s, part, position=1, **fields):
    from reprintbuddy.models import PartSupplier

    values = dict(name="Example Supplier B", order_url="https://shop.example/b", pack_size=10,
                  min_order_packs=1, lead_time_days=1, is_default=False)
    values.update(fields)
    row = PartSupplier(part_id=part.id, position=position, **values)
    s.add(row)
    s.flush()
    return row


def test_every_supplier_gets_its_own_quantity_default_first(db):
    """The end-to-end example of the spec: stock 0, minimum 10, A pack 50 (default), B pack 10."""
    with db.begin() as s:
        part = make_part(s, stock=0, min_stock=10, pack_size=50, lead_time_days=3, supplier="Example Supplier")
        add_supplier(s, part, position=0 + 1)
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert [(o.name, o.packs, o.pack_size, o.lead_days, o.is_default) for o in sug.options] == [
            ("Example Supplier", 1, 50, 3, True), ("Example Supplier B", 1, 10, 1, False),
        ]
        assert (sug.packs, sug.pack_size, sug.supplier) == (1, 50, "Example Supplier")


def test_the_default_comes_first_wherever_it_stands(db):
    with db.begin() as s:
        part = make_part(s, stock=0, min_stock=10, pack_size=50, supplier="Example Supplier")
        a = catalog.default_supplier(s, part.id)
        a.is_default = False
        s.flush()
        add_supplier(s, part, is_default=True)
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert [o.name for o in sug.options] == ["Example Supplier B", "Example Supplier"]
        assert (sug.pack_size, sug.supplier, sug.order_url) == (10, "Example Supplier B", "https://shop.example/b")


def test_each_quantity_uses_that_suppliers_lead_time_pack_and_minimum(db):
    """Usage 1 a day: A (lead 10, pack 50) needs 10 + 10 + 6 - 10 = 16 → 1 × 50;
    B (lead 2, pack 4, at least 3 packs) needs 10 + 2 + 6 - 10 = 8 → 2 packs, at least 3."""
    with db.begin() as s:
        part = part_with_demand(s, safety_value=10, lead_time_days=10)
        add_supplier(s, part, pack_size=4, min_order_packs=3, lead_time_days=2)
        for back in range(30):
            journal(s, "part_consumed", part.id, 1, datetime(2026, 11, 5, 9) - timedelta(days=back))
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert [(o.packs, o.pack_size, o.lead_days) for o in sug.options] == [(1, 50, 10), (3, 4, 2)]


def test_only_the_default_decides_whether_a_suggestion_appears(db):
    """B's longer lead time alone makes no suggestion; A's makes one, and B is listed too."""
    with db.begin() as s:
        part = part_with_demand(s, stock=40, safety_value=20, lead_time_days=0)
        add_supplier(s, part, lead_time_days=30)
        for back in range(30):
            journal(s, "part_consumed", part.id, 1, datetime(2026, 11, 5, 9) - timedelta(days=back))
        assert reorder.suggestions(s, sorts=None, today=TODAY) == []  # 40 - 6 - 0 >= 20
        catalog.default_supplier(s, part.id).lead_time_days = 30  # 40 - 6 - 30 < 20
        s.flush()
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert len(sug.options) == 2


def test_a_part_with_one_supplier_suggests_as_before(db):
    with db.begin() as s:
        part_with_demand(s)
        (sug,) = reorder.suggestions(s, sorts=None, today=TODAY)
        assert [(o.packs, o.pack_size) for o in sug.options] == [(1, 50)]
        assert (sug.packs, sug.pack_size, sug.order_url) == (1, 50, "https://shop.example/insert-m4")


def add_roll_supplier(s, sort, position=1, **fields):
    """A second supplier of a spool sort (Spec 9.3, p281): 500 g rolls, a day's delivery."""
    from reprintbuddy.models import FilamentSupplier

    values = dict(name="Roll Shop", order_url="https://rolls.example/pla", pack_size=500,
                  min_order_packs=1, lead_time_days=1, is_default=False)
    values.update(fields)
    row = FilamentSupplier(sort_id=sort.id, position=position, **values)
    s.add(row)
    s.flush()
    return row


def test_a_filament_suggestion_lists_every_supplier_default_first(db):
    """Spec 9.3: as for parts, each supplier of the sort with its own quantity, the default first.
    Stock 100 g, demand 170 g, safety 2000 g: 2070 g short → 3 × 1000 g or 5 × 500 g."""
    with db.begin() as s:
        sort = filament_setup(s, supplier="Example Shop", lead_time_days=3)
        add_roll_supplier(s, sort)
        with_demand(s, sort)
        (sug,) = reorder.suggestions(s, sorts=sorts_of((sort, 100.0)), today=TODAY)
        assert [(o.name, o.packs, o.pack_size, o.lead_days, o.is_default) for o in sug.options] == [
            ("Example Shop", 3, 1000, 3, True), ("Roll Shop", 5, 500, 1, False),
        ]
        assert (sug.packs, sug.pack_size, sug.lead_days, sug.supplier) == (3, 1000, 3, "Example Shop")


def test_only_the_default_filament_supplier_decides(db):
    """A second supplier with a long lead time alone makes no suggestion (Spec 9.3)."""
    with db.begin() as s:
        sort = filament_setup(s, safety_value=0, lead_time_days=0)
        add_roll_supplier(s, sort, lead_time_days=30)
        with_demand(s, sort)
        make_snapshot(s, TODAY - timedelta(days=10), 1, sort.id, 1300.0)
        make_snapshot(s, TODAY - timedelta(days=1), 1, sort.id, 400.0)  # 100 g a day
        assert reorder.suggestions(s, sorts=sorts_of((sort, 400.0)), today=TODAY) == []  # 400 - 170 >= 0
        filament.default_supplier(s, sort.id).lead_time_days = 30
        s.flush()
        (sug,) = reorder.suggestions(s, sorts=sorts_of((sort, 400.0)), today=TODAY)
        assert len(sug.options) == 2


def test_mark_with_a_supplier_keeps_its_pack_and_name(db):
    with db.begin() as s:
        part = part_with_demand(s)
        b = add_supplier(s, part)
        mark = reorder.mark_ordered(s, "part", part.id, 2, supplier_id=b.id)
        assert (mark.qty, mark.pack_size, mark.supplier_name) == (2, 10, "Example Supplier B")
        assert reorder.ordered_amount(s, mark) == 20
        assert reorder.on_way(s) == {("part", part.id): 20.0}
        (row,) = reorder.open_marks(s)
        assert (row.packs, row.pack_size, row.supplier) == (2, 10, "Example Supplier B")


def test_mark_without_a_supplier_takes_the_default(db):
    with db.begin() as s:
        part = part_with_demand(s)
        add_supplier(s, part)
        mark = reorder.mark_ordered(s, "part", part.id, 1)
        assert (mark.pack_size, mark.supplier_name) == (50, "Example Supplier")


def test_the_amount_on_its_way_stays_as_ordered(db):
    """Spec 9.4: changing or removing the supplier later changes nothing on its way."""
    with db.begin() as s:
        part = part_with_demand(s)
        b = add_supplier(s, part)
        reorder.mark_ordered(s, "part", part.id, 2, supplier_id=b.id)
        b.pack_size = 25
        s.flush()
        assert reorder.on_way(s) == {("part", part.id): 20.0}
        s.delete(b)
        s.flush()
        assert reorder.on_way(s) == {("part", part.id): 20.0}
        assert reorder.open_marks(s)[0].supplier == "Example Supplier B"


def test_a_supplier_of_another_part_is_not_found(db):
    with db.begin() as s:
        part = part_with_demand(s)
        other = make_part(s, name="Screw")
        foreign = add_supplier(s, other)
        with pytest.raises(NotFoundError) as info:
            reorder.mark_ordered(s, "part", part.id, 1, supplier_id=foreign.id)
        assert info.value.key == "reorder.supplier_not_found"
        assert s.query(ReorderMark).count() == 0


def test_a_filament_mark_without_a_supplier_takes_the_default(db):
    with db.begin() as s:
        sort = filament_setup(s, supplier="Example Shop")
        add_roll_supplier(s, sort)
        mark = reorder.mark_ordered(s, "filament", sort.id, 2)
        assert (mark.pack_size, mark.supplier_name, mark.supplier_position) == (1000, "Example Shop", 0)
        assert reorder.ordered_amount(s, mark) == 2000


def test_a_filament_mark_with_a_supplier_keeps_its_roll_size(db):
    """Spec 9.4, p281: the amount on its way stays as ordered, also for filament."""
    with db.begin() as s:
        sort = filament_setup(s)
        b = add_roll_supplier(s, sort)
        reorder.mark_ordered(s, "filament", sort.id, 2, supplier_id=b.id)
        assert reorder.on_way(s, []) == {("filament", sort.id): 1000.0}
        (row,) = reorder.open_marks(s, [])
        assert (row.packs, row.pack_size, row.supplier, row.supplier_position) == (2, 500, "Roll Shop", 1)
        b.pack_size = 750
        s.flush()
        assert reorder.on_way(s, []) == {("filament", sort.id): 1000.0}
        s.delete(b)
        s.flush()
        assert reorder.on_way(s, []) == {("filament", sort.id): 1000.0}


def test_a_filament_mark_refuses_a_foreign_supplier_and_an_outdated_page(db):
    with db.begin() as s:
        sort = filament_setup(s)
        other = filament_setup(s, make_sort(s, brand="Brand B"))
        foreign = add_roll_supplier(s, other)
        with pytest.raises(NotFoundError) as info:
            reorder.mark_ordered(s, "filament", sort.id, 1, supplier_id=foreign.id)
        assert info.value.key == "reorder.supplier_not_found"
        seen = reorder.suppliers_key(s, "filament", sort.id)
        add_roll_supplier(s, sort)
        with pytest.raises(ConflictError) as changed:
            reorder.mark_ordered(s, "filament", sort.id, 1, suppliers_seen=seen)
        assert changed.value.key == "reorder.suppliers_changed"
        assert s.query(ReorderMark).count() == 0


def test_a_filament_mark_closes_by_new_spools_of_any_shop(db):
    """What has arrived is the nominal weight of new spools of the sort, whoever sold them; nothing
    is booked for filament (Spec 9.1, 9.4)."""
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s)
        b = add_roll_supplier(s, sort)
        mark = reorder.mark_ordered(s, "filament", sort.id, 2, supplier_id=b.id)  # 2 x 500 g
        after = mark.marked_at + timedelta(hours=1)
        spools = [fake.add_spool("PLA", brand="Brand A", created_at=after, label_g=1000)]
        filament.sync_sorts(s, spools)
        before = s.query(JournalEntry).count()
        assert reorder.close_arrived_marks(s, spools, NOW) == 1
        assert s.query(JournalEntry).count() == before


def test_still_one_open_mark_per_part(db):
    with db.begin() as s:
        part = part_with_demand(s)
        b = add_supplier(s, part)
        reorder.mark_ordered(s, "part", part.id, 1)
        with pytest.raises(ConflictError):
            reorder.mark_ordered(s, "part", part.id, 1, supplier_id=b.id)



def test_a_mark_keeps_the_position_of_its_supplier(db):  # review R2: "Supplier 2" without a name
    with db.begin() as s:
        part = part_with_demand(s)
        b = add_supplier(s, part, name=None)
        mark = reorder.mark_ordered(s, "part", part.id, 1, supplier_id=b.id)
        assert (mark.supplier_name, mark.supplier_position) == (None, 1)
        (row,) = reorder.open_marks(s)
        assert (row.supplier, row.supplier_position) == (None, 1)


def test_closing_a_filament_mark_counts_through_ordered_amount(db, monkeypatch):  # review R4
    with db.begin() as s:
        sort = filament_setup(s)
        reorder.mark_ordered(s, "filament", sort.id, 2)
        seen = []
        real = reorder.ordered_amount
        monkeypatch.setattr(reorder, "ordered_amount", lambda session, mark: seen.append(mark.id) or real(session, mark))
        reorder.close_arrived_marks(s, [], NOW)
        assert seen


# ---- p211: what has arrived no longer counts as on its way (Spec 9.4) ---------------------------


def test_new_spools_of_a_partial_delivery_are_taken_off_on_its_way(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s)
        mark = reorder.mark_ordered(s, "filament", sort.id, 3)  # 3000 g ordered
        after = mark.marked_at + timedelta(hours=1)
        spools = [
            fake.add_spool("PLA", brand="Brand A", created_at=mark.marked_at - timedelta(days=1)),
            fake.add_spool("PLA", brand="Brand A", created_at=after, remaining_g=400.0),  # used: still arrived
            fake.add_spool("PLA", brand="Brand A", created_at=after),
        ]
        filament.sync_sorts(s, spools)
        assert reorder.close_arrived_marks(s, spools, NOW) == 0
        assert reorder.on_way(s, spools) == {("filament", sort.id): 1000.0}
        (row,) = reorder.open_marks(s, spools)
        assert row.arrived == 2000.0


def test_a_partial_delivery_counts_from_the_record_without_bambuddy(db):
    with db.begin() as s:
        sort = filament_setup(s)
        mark = reorder.mark_ordered(s, "filament", sort.id, 3)
        make_snapshot(s, TODAY, 1, sort.id, 900.0, spool_created_at=mark.marked_at + timedelta(hours=1))
        make_snapshot(s, TODAY, 2, sort.id, 900.0, spool_created_at=mark.marked_at - timedelta(days=2))
        make_snapshot(s, TODAY - timedelta(days=1), 3, sort.id, 900.0,
                      spool_created_at=mark.marked_at + timedelta(hours=1))  # an older day does not count
        assert reorder.on_way(s, None) == {("filament", sort.id): 2000.0}


def test_on_its_way_never_goes_below_zero(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        sort = filament_setup(s)
        mark = reorder.mark_ordered(s, "filament", sort.id, 1)
        big = fake.add_spool("PLA", brand="Brand A", created_at=mark.marked_at + timedelta(hours=1), label_g=5000)
        filament.sync_sorts(s, [big])
        assert reorder.on_way(s, [big]).get(("filament", sort.id), 0.0) == 0.0


def test_suggestion_counts_only_the_rest_on_its_way(db):
    """End to end over `current`: 2 of 3 rolls of B arrived, only 1000 g of B is still on its way."""
    fake = FakeBambuddy()
    with db.begin() as s:
        a = filament_setup(s, safety_value=4500)
        b = filament_setup(s, make_sort(s, brand="Brand B"))
        with_demand(s, a, b, name="One")
        mark = reorder.mark_ordered(s, "filament", b.id, 3)
        after = mark.marked_at + timedelta(hours=1)
        spools = [
            fake.add_spool("PLA", brand="Brand A", remaining_g=300.0),
            fake.add_spool("PLA", brand="Brand B", created_at=after),
            fake.add_spool("PLA", brand="Brand B", created_at=after),
        ]
        filament.sync_sorts(s, spools)
        # the group of A: stock 300 + 2000; with all 3000 g on its way 5300 - 170 >= 4500 hid it
        (sug,) = reorder.current(s, spools, NOW).suggestions
        assert (sug.item_id, sug.stock, sug.on_way, sug.packs) == (a.id, 2300.0, 1000.0, 2)


def test_a_part_mark_stays_open_until_the_ordered_amount_is_in(db):
    """Sister path: a first receipt of 20 of 50 no longer closes the mark; 30 stay on their way."""
    with db.begin() as s:
        part = part_with_demand(s)
        mark = reorder.mark_ordered(s, "part", part.id, 1)  # 50
        mark.marked_at -= timedelta(seconds=1)
        s.flush()
        stock.receive_part(s, part.id, 20, "user")
        assert reorder.close_arrived_marks(s, None, NOW) == 0
        assert reorder.on_way(s, None) == {("part", part.id): 30.0}
        (row,) = reorder.open_marks(s)
        assert row.arrived == 20
        stock.receive_part(s, part.id, 30, "user")
        assert reorder.close_arrived_marks(s, None, NOW) == 1
        assert reorder.on_way(s, None) == {}


# ---- p262: a receipt counts from the day of the mark on (Spec 9.4) ------------------------------
# The tests pin the display zone to Europe/Berlin (conftest), UTC+1 in November.


def _marked_part(s, marked_at):
    part = part_with_demand(s)
    mark = reorder.mark_ordered(s, "part", part.id, 1)  # 50
    mark.marked_at = marked_at
    s.flush()
    return part, mark


def test_a_receipt_dated_before_the_day_of_the_mark_does_not_count(db):
    """Booked after the mark, but dated the day before: it is no delivery of this order."""
    with db.begin() as s:
        part, _mark = _marked_part(s, datetime(2026, 11, 6, 10, 0))
        stock.receive_part(s, part.id, 50, "user", booked_at=datetime(2026, 11, 5, 10, 0))
        assert reorder.on_way(s, None) == {("part", part.id): 50.0}
        assert reorder.open_marks(s)[0].arrived == 0
        assert reorder.close_arrived_marks(s, None, NOW) == 0


def test_a_receipt_dated_on_the_day_of_the_mark_counts_whatever_the_hour(db):
    """The day decides, not the hour: a dated receipt has no real time of its own."""
    with db.begin() as s:
        part, mark = _marked_part(s, datetime(2026, 11, 6, 10, 0))
        stock.receive_part(s, part.id, 20, "user", booked_at=datetime(2026, 11, 6, 8, 0))
        assert reorder.on_way(s, None) == {("part", part.id): 30.0}
        stock.receive_part(s, part.id, 30, "user")  # today
        assert reorder.close_arrived_marks(s, None, NOW) == 1
        assert mark.received_at is not None


def test_the_day_is_the_one_of_the_display_zone(db):
    """Marked 00:30 on 7 Nov in Berlin (23:30 UTC on the 6th); a receipt dated 23:30 on 6 Nov in
    Berlin (22:30 UTC) lies a day before, though both are the 6th in UTC."""
    with db.begin() as s:
        part, _mark = _marked_part(s, datetime(2026, 11, 6, 23, 30))
        stock.receive_part(s, part.id, 50, "user", booked_at=datetime(2026, 11, 6, 22, 30))
        assert reorder.on_way(s, None) == {("part", part.id): 50.0}
        stock.receive_part(s, part.id, 50, "user", booked_at=datetime(2026, 11, 6, 23, 10))  # 00:10 on the 7th
        assert reorder.on_way(s, None) == {}
