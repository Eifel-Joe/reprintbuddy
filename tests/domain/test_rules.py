from datetime import date

from reprintbuddy.domain.rules import (
    PositionStock,
    available,
    consumption_per_day,
    coverage,
    needs_reorder,
    reorder_floor,
    order_ready,
    prints_for_units,
    prints_needed,
    safety_amount,
    set_available,
    suggest_packs,
    target_quantity,
)


def test_available():
    assert available(5, 2) == 3
    assert available(1, 3) == -2


def test_target_quantity():
    assert target_quantity(4, 2) == 6  # Spec 7.1


def test_set_minimum():  # Spec 5.4
    assert set_available([PositionStock(1, (3,)), PositionStock(2, (5,))]) == 2


def test_set_choice_takes_the_smallest_option():  # Spec 5.4, v0.3.16 p173: the options are not added up
    assert set_available([PositionStock(1, (3, 5))]) == 3
    assert set_available([PositionStock(1, (3, 5)), PositionStock(1, (4,))]) == 3
    assert set_available([PositionStock(1, (5, 3))]) == 3  # the order of the options does not matter
    assert set_available([PositionStock(2, (7, 9))]) == 3  # 7 // 2
    assert set_available([PositionStock(1, (3, 0))]) == 0  # one option empty: the set is not deliverable


def test_set_choice_option_shared_with_a_fixed_position():  # review of p173
    """The article that is fixed in one position and an option in another needs both quantities when the
    customer picks it: the worst choice counts."""
    fixed = PositionStock(1, (5,), (1,))
    choice = PositionStock(1, (5, 10), (1, 2))
    assert set_available([fixed, choice]) == 2  # picking article 1 needs two of it per set: 5 // 2
    assert set_available([PositionStock(1, (5,), (1,)), PositionStock(1, (9, 10), (2, 3))]) == 5  # nothing shared


def test_set_choice_options_shared_between_two_choice_positions():  # review of p173
    first = PositionStock(1, (4, 10), (1, 2))
    second = PositionStock(1, (4, 10), (1, 3))
    assert set_available([first, second]) == 2  # article 1 picked in both positions: 4 // 2


def test_set_shared_option_with_a_negative_stock_counts_the_worst_choice():  # review of p173
    first = PositionStock(1, (-2, 3), (1, 2))
    second = PositionStock(1, (-2, 3), (1, 2))
    assert set_available([first, second]) == -2  # picked once, article 1 is short by 2
    assert set_available([PositionStock(1, (-1,), (7,)), PositionStock(1, (-1, 4), (7, 8))]) == -1


def test_set_negative():
    assert set_available([PositionStock(1, (-1,))]) == -1


def test_set_choice_with_a_negative_option():
    """An option that is oversold pulls the position down like a fixed one (it was ignored while the options were added)."""
    assert set_available([PositionStock(1, (-2, 3))]) == -2
    assert set_available([PositionStock(1, (-2, 3)), PositionStock(1, (4,))]) == -2


def test_set_empty():
    assert set_available([]) == 0
    assert set_available([PositionStock(1, (5,)), PositionStock(1, ())]) == 0


def test_order_ready():  # Spec 6.7
    assert order_ready({1: 2}, {1: 2}) is True
    assert order_ready({1: 3}, {1: 2}) is False
    assert order_ready({1: 1}, {}) is False


def test_set_same_article_in_two_fixed_positions_adds_up():  # Spec 5.4 "gleichzeitig"
    positions = [PositionStock(1, (3,), (7,)), PositionStock(1, (3,), (7,))]
    assert set_available(positions) == 1
    assert set_available([PositionStock(2, (7,), (7,)), PositionStock(1, (7,), (7,))]) == 2  # 7 // 3
    assert set_available([PositionStock(1, (3,), (7,)), PositionStock(1, (9,), (8,))]) == 3  # different articles
    assert set_available([PositionStock(1, (-1,), (7,)), PositionStock(1, (-1,), (7,))]) == -1


def test_coverage_sum():  # Spec 7.1
    assert coverage(3, 2, 1, 1, 1) == 8
    assert coverage(-2, 0, 0, 0, 0) == -2


def test_prints_needed_spec_example():  # Spec 7.1
    assert prints_needed(min_stock=4, buffer=2, coverage=3, units_per_print=1) == 3


def test_prints_needed_at_or_above_min():  # Spec 7.1
    assert prints_needed(min_stock=4, buffer=2, coverage=4, units_per_print=1) == 0
    assert prints_needed(min_stock=4, buffer=2, coverage=5, units_per_print=1) == 0


def test_prints_needed_rounds_up():  # Spec 7.1
    assert prints_needed(min_stock=4, buffer=1, coverage=0, units_per_print=2) == 3


def test_prints_needed_never_for_zero_zero():  # Spec 5.2
    assert prints_needed(min_stock=0, buffer=0, coverage=-3, units_per_print=1) == 0


def test_prints_needed_min_zero_with_buffer():  # Entscheidung 19
    assert prints_needed(min_stock=0, buffer=2, coverage=-1, units_per_print=1) == 3


def test_prints_for_units():  # Spec 7.5
    assert prints_for_units(0, 1) == 0
    assert prints_for_units(1, 1) == 1
    assert prints_for_units(3, 2) == 2
    assert prints_for_units(2, 2) == 1


def _d(n: int) -> date:
    return date(2026, 11, n)


def test_consumption_simple():  # Entscheidung 7
    series = {1: [(_d(1), 1000.0), (_d(2), 900.0), (_d(3), 750.0)]}
    assert consumption_per_day(series) == (125.0, 2)


def test_consumption_ignores_increases_and_new_spools():
    series = {
        1: [(_d(1), 500.0), (_d(2), 400.0), (_d(3), 450.0), (_d(4), 350.0)],
        2: [(_d(3), 1000.0), (_d(4), 1000.0)],
    }
    assert consumption_per_day(series) == (200 / 3, 3)


def test_consumption_needs_two_days():
    assert consumption_per_day({1: [(_d(1), 500.0)]}) == (0.0, 0)
    assert consumption_per_day({}) == (0.0, 0)


def test_safety_amount():  # Entscheidung 11
    assert safety_amount(5, "days", 20.0) == 100.0
    assert safety_amount(300, "grams", 20.0) == 300
    assert safety_amount(10, "qty", 2.0) == 10


def test_needs_reorder():  # Spec 9.3
    kw = dict(stock=1100, demand=200, per_day=50, lead_days=4)
    assert needs_reorder(**kw, safety=500) is False  # 1100 - 200 - 200 = 700
    assert needs_reorder(**kw, safety=800) is True


def test_reorder_floor_is_the_larger_value():  # Spec 9.3
    assert reorder_floor(30.0, 20) == 30.0
    assert reorder_floor(5.0, 20) == 20.0
    assert reorder_floor(0.0, 0) == 0.0


def test_suggest_packs_rounds_to_pack():  # Spec 9.3
    kw = dict(stock=10, demand=6, per_day=0, lead_days=0, pack=50, min_packs=1)
    assert suggest_packs(**kw, safety=20) == 1
    assert suggest_packs(**kw, safety=70) == 2


def test_suggest_packs_min_order():
    result = suggest_packs(
        stock=10, demand=0, per_day=0, lead_days=0, safety=20, pack=50, min_packs=3
    )
    assert result == 3


def test_suggest_packs_float_safe():
    result = suggest_packs(
        stock=0, demand=0, per_day=0, lead_days=0, safety=1000.0000000001, pack=1000, min_packs=1
    )
    assert result == 1
