"""How many days a reordered spool sort lasts, for the overview's filament tile (Spec 9.4, 10.2, 0.4.2).

Reichweite = (stock + on the way - planned demand) / usage per day (Spec 9.2), rounded down, never below
0; without usage in the last 30 days none. Less than 30 days of usage known: "young" (the tile says so).
"""

from datetime import date, datetime, timedelta

from factories import assign_sort, make_filament_settings, make_snapshot, make_sort
from reprintbuddy.domain import reorder, rules

TODAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 6, 12, 0)


def test_reach_example_of_the_spec():
    assert rules.reach_days(stock=1200, coming=400, demand=800, per_day=160) == 5


def test_never_below_zero():
    assert rules.reach_days(stock=100, coming=0, demand=800, per_day=50) == 0


def test_no_usage_is_none():
    assert rules.reach_days(stock=1200, coming=0, demand=0, per_day=0.0) is None


def _sort_with_usage(s, *, brand, start, end, days_back, grams=100.0, target=8, spool_id=1):
    """A reordered sort of one spool used from `start` to `end` g over `days_back` days; its article
    has `grams` per print and is `target` short of its target (planned demand = grams x target)."""
    sort = make_sort(s, brand=brand)
    assign_sort(s, sort, name=f"Article {brand}", grams=grams, min_stock=target, buffer=0, stock=0)
    make_snapshot(s, TODAY - timedelta(days=days_back), spool_id, sort.id, start)
    make_snapshot(s, TODAY, spool_id, sort.id, end)
    return sort


def test_reach_from_the_record_young_and_sorted(db):
    with db.begin() as s:
        lasting = _sort_with_usage(s, brand="Brand L", start=10000.0, end=9000.0, days_back=30, grams=10.0, spool_id=1)
        short = _sort_with_usage(s, brand="Brand S", start=2800.0, end=1200.0, days_back=10, grams=50.0, spool_id=2)
        idle = make_sort(s, brand="Brand I")
        assign_sort(s, idle, name="Article idle", grams=5.0, min_stock=0, buffer=0, stock=5)
        make_snapshot(s, TODAY, 3, idle.id, 500.0)
        current = reorder.current(s, None, NOW, write=False)
        found = reorder.filament_reach(s, current, TODAY)
    assert [r.sort_id for r in found] == [short.id, lasting.id, idle.id]
    by = {r.sort_id: r for r in found}
    assert by[short.id].days == (1200 - 400) // 160  # 50 g x 8 short of the target
    assert by[short.id].young is True
    assert by[lasting.id].days == (9000 - 80) * 30 // 1000 and by[lasting.id].young is False
    assert by[idle.id].days is None


def test_short_follows_the_suggestions(db):
    with db.begin() as s:
        sort = _sort_with_usage(s, brand="Brand S", start=2800.0, end=1200.0, days_back=10, grams=50.0)
        make_filament_settings(s, sort.id, pack_grams=1000, min_order_packs=1, safety_value=2000, safety_unit="grams")
        other = _sort_with_usage(s, brand="Brand O", start=1500.0, end=1400.0, days_back=10, grams=1.0, spool_id=2)
        current = reorder.current(s, None, NOW, write=False)
        found = {r.sort_id: r for r in reorder.filament_reach(s, current, TODAY)}
    assert found[sort.id].short is True
    assert found[other.id].short is False


def test_unknown_stock_gives_nothing(db):
    with db.begin() as s:
        _sort_with_usage(s, brand="Brand S", start=2800.0, end=1200.0, days_back=10)
        current = reorder.Current(None, None, [])
        assert reorder.filament_reach(s, current, TODAY) == []
