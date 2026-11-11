import itertools
import random
from datetime import date

from reprintbuddy.domain import rules
from reprintbuddy.domain.rules import (
    Bundle,
    ChoiceStock,
    SetStock,
    available,
    bundle_availability,
    consumption_per_day,
    coverage,
    needs_reorder,
    reorder_floor,
    order_ready,
    prints_for_units,
    prints_needed,
    safety_amount,
    set_availability,
    suggest_packs,
    target_quantity,
    variant_number,
)


def test_available():
    assert available(5, 2) == 3
    assert available(1, 3) == -2


def test_target_quantity():
    assert target_quantity(4, 2) == 6  # Spec 7.1


def test_set_without_choices_is_the_minimum_over_its_articles():  # Spec 5.4
    result = set_availability([(1, 4), (2, 5)], [])
    assert result == SetStock(2, {})


def test_all_one_each_gives_one_per_variant_and_one_together():  # Spec 5.4, first example
    result = set_availability([(1, 1)], [ChoiceStock(1, ((10, 1), (11, 1), (12, 1)))])
    assert result.variants == {(10,): 1, (11,): 1, (12,): 1}
    assert result.total == 1


def test_base_three_choices_two_zero_one_together_three():  # Spec 5.4, second example
    result = set_availability([(1, 3)], [ChoiceStock(1, ((10, 2), (11, 0), (12, 1)))])
    assert result.variants == {(10,): 2, (11,): 0, (12,): 1}
    assert result.total == 3


def test_two_fields_give_the_cross_product():  # Spec 5.4: Wahlteile 1 x Wahlteile 2
    result = set_availability(
        [(1, 10)], [ChoiceStock(1, ((10, 2), (11, 1))), ChoiceStock(1, ((20, 1), (21, 5)))]
    )
    assert result.variants == {(10, 20): 1, (10, 21): 2, (11, 20): 1, (11, 21): 1}
    assert list(result.variants) == [(10, 20), (10, 21), (11, 20), (11, 21)]  # the order of the form
    assert result.total == 3  # min(10, 2 + 1, 1 + 5)


def test_choice_quantity_divides_each_option():  # one quantity per field (decision p340, question 1)
    result = set_availability([], [ChoiceStock(2, ((10, 5), (11, 1)))])
    assert result.variants == {(10,): 2, (11,): 0}
    assert result.total == 2


def test_oversold_option_does_not_lower_the_others():  # Spec 6.4: Verfuegbar may be negative
    result = set_availability([(1, 9)], [ChoiceStock(1, ((10, -2), (11, 3)))])
    assert result.variants == {(10,): -2, (11,): 3}
    assert result.total == 3
    assert set_availability([(1, 9)], [ChoiceStock(1, ((10, -2),))]).total == -2
    assert set_availability([(1, -1)], [ChoiceStock(1, ((10, 4),))]) == SetStock(-1, {(10,): -1})


def test_only_choice_fields_without_base():
    result = set_availability([], [ChoiceStock(1, ((10, 2),)), ChoiceStock(1, ((20, 1), (21, 1)))])
    assert result.total == 2
    assert result.variants == {(10, 20): 1, (10, 21): 1}


def test_empty_field_gives_zero():  # every choice archived (Spec 5.5)
    assert set_availability([(1, 5)], [ChoiceStock(1, ())]) == SetStock(0, {})
    assert set_availability([], []) == SetStock(0, {})


def _most_sets(base, choices) -> int:
    """Brute force: the largest n for which n sets in some mix of the variants come from the stock."""
    n = 0
    while True:
        wanted = n + 1
        if any(a < wanted * q for q, a in base):
            return n
        for field in choices:  # the fields hold different articles, so each is filled on its own
            if sum(max(a, 0) // field.qty for _, a in field.options) < wanted:
                return n
        n = wanted


def test_together_is_the_most_sets_in_any_mix():  # Spec 5.4 "zusammen hoechstens"
    rng = random.Random(340)
    for _ in range(300):
        base = [(rng.randint(1, 3), rng.randint(0, 9)) for _ in range(rng.randint(0, 2))]
        ids = iter(range(100))
        choices = [
            ChoiceStock(rng.randint(1, 2), tuple((next(ids), rng.randint(0, 9)) for _ in range(rng.randint(1, 3))))
            for _ in range(rng.randint(1, 2))
        ]
        assert set_availability(base, choices).total == _most_sets(base, choices), (base, choices)



# 0.4.0 (p341): availability of bundles, the generalisation of set_availability for articles made of
# parts, where one option may hold several articles and an article may sit in several options of a field.


def test_bundles_without_shared_articles_match_set_availability():  # Spec 5.4, second example
    result = bundle_availability(
        [(1, 1)], [[Bundle(10, ((10, 1),)), Bundle(11, ((11, 1),)), Bundle(12, ((12, 1),))]],
        {1: 3, 10: 2, 11: 0, 12: 1},
    )
    assert result == SetStock(3, {(10,): 2, (11,): 0, (12,): 1})
    assert result.capped is False


def _hose(segment: int):
    return bundle_availability(
        [(1, 12)],
        [[
            Bundle(11, ((2, 1), (4, 1))),
            Bundle(12, ((2, 1), (5, 1))),
            Bundle(13, ((3, 1), (6, 1))),
            Bundle(14, ((3, 1), (7, 1))),
        ]],
        {1: segment, 2: 3, 3: 1, 4: 2, 5: 2, 6: 0, 7: 1},
    )


def test_hose_example_counts_shared_side_part_once():  # Spec 5.6 example
    result = _hose(60)
    assert result.variants == {(11,): 2, (12,): 2, (13,): 0, (14,): 1}
    assert result.total == 4  # side part L only three times: -L32 and -L40 together 3, not 4


def test_base_limits_the_hose_together():
    assert _hose(24).total == 2


def test_option_with_quantity_two():
    result = bundle_availability([], [[Bundle(10, ((2, 2),))]], {2: 5})
    assert result == SetStock(2, {(10,): 2})


def test_two_fields_with_shared_articles_inside_each_field():
    result = bundle_availability(
        [(1, 1)],
        [
            [Bundle(21, ((2, 1), (3, 1))), Bundle(22, ((2, 1), (4, 1)))],
            [Bundle(31, ((5, 1), (6, 1))), Bundle(32, ((5, 2), (7, 1)))],
        ],
        {1: 10, 2: 3, 3: 2, 4: 2, 5: 5, 6: 3, 7: 5},
    )
    assert result.variants == {(21, 31): 2, (21, 32): 2, (22, 31): 2, (22, 32): 2}
    assert result.total == 3  # field 1 gives 3, field 2 gives 4


def test_oversold_shared_article():  # Spec 6.4: Verfuegbar may be negative
    result = bundle_availability(
        [(1, 1)], [[Bundle(11, ((2, 1), (3, 1))), Bundle(12, ((2, 1), (4, 3)))]], {1: 9, 2: -1, 3: 5, 4: 5}
    )
    assert result == SetStock(-1, {(11,): -1, (12,): -1})


def test_empty_field_or_nothing_gives_zero():
    assert bundle_availability([(1, 1)], [[]], {1: 5}) == SetStock(0, {})
    assert bundle_availability([], [], {}) == SetStock(0, {})


def test_search_limit_returns_a_lower_bound(monkeypatch):
    # five options in a ring, each sharing one article with the next: at most 2 together
    ring = [Bundle(10 + i, ((i, 1), ((i + 1) % 5, 1))) for i in range(5)]
    stock = {i: 1 for i in range(5)}
    assert bundle_availability([], [ring], stock) == SetStock(2, {(10 + i,): 1 for i in range(5)})
    monkeypatch.setattr(rules, "MAX_NODES", 2)
    capped = bundle_availability([], [ring], stock)
    assert capped.capped is True
    assert 1 <= capped.total <= 2  # never below the best single option, never above the truth
    assert capped.variants == {(10 + i,): 1 for i in range(5)}


def _most_bundles(base, field, stock) -> int:
    """Brute force: the largest n for which n articles in some mix of the options come from the stock."""
    best = 0
    limits = [max(0, min(stock[a] // q for a, q in b.needs)) for b in field]
    for counts in itertools.product(*(range(limit + 1) for limit in limits)):
        used = {}
        for bundle, y in zip(field, counts):
            for a, q in bundle.needs:
                used[a] = used.get(a, 0) + y * q
        if all(used[a] <= stock[a] for a in used):
            best = max(best, sum(counts))
    return min([best] + [stock[a] // q for a, q in base])


def test_together_is_the_most_in_any_mix_with_shared_articles():  # Spec 5.6 "zusammen hoechstens"
    rng = random.Random(341)
    for _ in range(300):
        stock = {a: rng.randint(0, 6) for a in range(1, 8)}
        base = [(1, rng.randint(1, 2))] if rng.random() < 0.5 else []
        field = [
            Bundle(100 + i, tuple((a, rng.randint(1, 2)) for a in rng.sample(range(2, 8), rng.randint(1, 3))))
            for i in range(rng.randint(1, 4))
        ]
        if not any(min(stock[a] // q for a, q in b.needs) > 0 for b in field):
            continue  # all options at zero: covered by the oversold test
        assert bundle_availability(base, [field], stock).total == _most_bundles(base, field, stock), (base, field, stock)


def test_order_ready():  # Spec 6.7
    assert order_ready({1: 2}, {1: 2}) is True
    assert order_ready({1: 3}, {1: 2}) is False
    assert order_ready({1: 1}, {}) is False


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


def test_variant_number_writes_the_suffixes_behind_the_number():  # Spec 5.4: as they are, no separator
    assert variant_number("EX-S10", ["-A", "-1"]) == "EX-S10-A-1"
    assert variant_number("EX-S10", ["A"]) == "EX-S10A"
    assert variant_number("EX-S10", []) == "EX-S10"


def test_variant_number_is_none_while_a_part_is_missing():
    assert variant_number(None, ["-A"]) is None
    assert variant_number("", ["-A"]) is None
    assert variant_number("EX-S10", ["-A", None]) is None
    assert variant_number("EX-S10", ["-A", ""]) is None
