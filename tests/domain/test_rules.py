from reprintbuddy.domain.rules import (
    PositionStock,
    available,
    coverage,
    order_ready,
    prints_for_units,
    prints_needed,
    set_available,
    target_quantity,
)


def test_available():
    assert available(5, 2) == 3
    assert available(1, 3) == -2


def test_target_quantity():
    assert target_quantity(4, 2) == 6  # Spec 7.1


def test_set_minimum():  # Spec 5.4
    assert set_available([PositionStock(1, (3,)), PositionStock(2, (5,))]) == 2


def test_set_choice_sums():
    assert set_available([PositionStock(1, (3, 5))]) == 8
    assert set_available([PositionStock(1, (3, 5)), PositionStock(1, (4,))]) == 4


def test_set_negative():
    assert set_available([PositionStock(1, (-1,))]) == -1


def test_set_choice_ignores_negative():
    assert set_available([PositionStock(1, (-2, 3))]) == 3


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
