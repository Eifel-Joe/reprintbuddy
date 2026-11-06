import pytest

from reprintbuddy.domain.status import (
    STATES,
    article_status,
    order_status,
    part_status,
    set_status,
    summarize,
    tone,
)


def test_overbooked_whenever_available_is_negative():
    assert article_status(available=-1, min_stock=0, coverage=5) == "overbooked"
    assert article_status(available=-1, min_stock=4, coverage=0) == "overbooked"  # first row wins


def test_below_min_when_coverage_does_not_reach_min():
    assert article_status(available=1, min_stock=4, coverage=3) == "below_min"


def test_low_when_coverage_reaches_min():
    assert article_status(available=1, min_stock=4, coverage=4) == "low"
    assert article_status(available=0, min_stock=4, coverage=9) == "low"


def test_ok_when_available_reaches_min():
    assert article_status(available=4, min_stock=4, coverage=4) == "ok"
    assert article_status(available=0, min_stock=0, coverage=0) == "ok"


def test_part_set_and_order_status():
    assert part_status(stock=5, min_stock=5) == "ok"
    assert part_status(stock=4, min_stock=5) == "below_min"
    assert part_status(stock=0, min_stock=0) == "ok"
    assert set_status(available=1) == "deliverable"
    assert set_status(available=0) == "not_deliverable"
    assert set_status(available=-1) == "not_deliverable"
    assert order_status(status="open", ready=True) == "ready"
    assert order_status(status="open", ready=False) == "open"
    assert order_status(status="shipped", ready=True) == "shipped"
    assert order_status(status="cancelled", ready=False) == "cancelled"


def test_tone_of_every_state():
    expected = {
        "overbooked": "bad",
        "below_min": "bad",
        "not_deliverable": "bad",
        "low": "warn",
        "open": "warn",
        "ok": "ok",
        "deliverable": "ok",
        "ready": "ok",
        "shipped": "neutral",
        "cancelled": "neutral",
        "paused": "neutral",
        "archived": "neutral",
    }
    states = {s for group in STATES.values() for s in group} | {"paused", "archived"}
    assert states == set(expected)
    for state, wanted in expected.items():
        assert tone(state) == wanted
    with pytest.raises(ValueError):
        tone("bogus")


def test_summarize_counts_only_present_states_in_order():
    shown = ("overbooked", "below_min", "low")
    assert summarize(["ok", "low", "overbooked", "low"], shown) == [("overbooked", 1), ("low", 2)]
