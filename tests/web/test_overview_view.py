"""The overview's tiles (Spec 10.2, since 0.4.2): orders, stock, filament, to do; and the state of the
Bambuddy connection in the head line.

Pure: no app, no database. The route counts with the functions of the pages the tiles lead to; this file
checks what the tiles make of the counts (order, numbers, texts, tone, where each part leads).
"""

import pytest

from reprintbuddy.domain.engine import EngineStatus
from reprintbuddy.domain.reorder import Reach
from reprintbuddy.web.overview_view import (
    CONNECTION_KEYS,
    STOCK_STATES,
    connection_state,
    filament_tile,
    orders_tile,
    stock_tile,
    todo_lines,
)


# ---- connection ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "state", "tone"),
    [
        (EngineStatus(configured=False), "not_configured", "neutral"),
        (EngineStatus(configured=True, reachable=None), "unknown", "neutral"),
        (EngineStatus(configured=True, reachable=True), "connected", "ok"),
        (EngineStatus(configured=True, reachable=False), "unreachable", "bad"),
        (EngineStatus(configured=True, reachable=True, poll_error="403"), "rejected", "bad"),
        # Not answering is the newer fact: a refusal kept from before does not hide it.
        (EngineStatus(configured=True, reachable=False, poll_error="403"), "unreachable", "bad"),
    ],
)
def test_connection_state(status, state, tone):
    assert connection_state(status) == (state, tone)
    assert CONNECTION_KEYS[state] == f"overview.connection.{state}"


# ---- orders -------------------------------------------------------------------------------


def test_orders_parts_lead_to_their_filters():
    tile = orders_tile("en", open_count=5, ready=2, new=1)
    assert (tile.count, tile.tone) == (5, "warn")
    assert [(p.key, p.count, p.text, p.href, p.tone) for p in tile.parts] == [
        ("ready", 2, "2 ready to ship", "/orders?status=ready", "ok"),
        ("waiting", 3, "3 waiting for stock", "/orders?status=open", "warn"),
    ]
    assert tile.new == "1 new"


def test_orders_at_zero_are_grey_without_parts():
    tile = orders_tile("en", open_count=0, ready=0, new=0)
    assert (tile.count, tile.tone, tile.parts, tile.new) == (0, "neutral", (), "")


# ---- stock (p420, p421) -------------------------------------------------------------------


def test_stock_counts_per_article_like_the_articles_page():
    """One status per row of the articles page; an article made of parts built on order (deliverable /
    not deliverable) has no stock to count. Each part leads to the filter of exactly its state (p420)."""
    tile = stock_tile("en", ["ok", "ok", "low", "below_min", "overbooked", "deliverable", "not_deliverable"])
    assert tile.total == 5
    assert tile.title == "Stock · 5 articles"
    assert [(p.key, p.count, p.href, p.tone) for p in tile.parts] == [
        ("ok", 2, "/articles?status=ok", "ok"),
        ("low", 1, "/articles?status=low", "warn"),
        ("below_min", 1, "/articles?status=below_min", "bad"),
        ("overbooked", 1, "/articles?status=overbooked", "bad"),
    ]
    assert [p.text for p in tile.parts] == ["2 enough", "1 low (reprint running)", "1 below minimum", "1 overbooked"]
    assert STOCK_STATES == ("ok", "low", "below_min", "overbooked")


def test_stock_leaves_out_states_that_are_not_there():
    tile = stock_tile("en", ["ok", "low"])
    assert [p.key for p in tile.parts] == ["ok", "low"]  # p421: low is shown


def test_stock_without_articles():
    tile = stock_tile("en", [])
    assert (tile.total, tile.parts, tile.title) == (0, (), "Stock · 0 articles")


# ---- filament -----------------------------------------------------------------------------


def reach(sort_id, days, *, short=False, young=False, label=None):
    return Reach(sort_id, label or f"Sort {sort_id}", days, short, young)


def test_filament_lists_the_three_shortest():
    rows = [reach(1, 5, short=True), reach(2, 23), reach(3, 40), reach(4, None)]
    tile = filament_tile("en", short=1, reach=rows)
    assert (tile.count, tile.tone) == (1, "warn")
    assert [(r.label, r.text, r.tone) for r in tile.rows] == [
        ("Sort 1", "lasts 5 days", "warn"), ("Sort 2", "lasts 23 days", "ok"), ("Sort 3", "lasts 40 days", "ok"),
    ]
    assert tile.young is False


def test_filament_one_day_no_usage_and_young_hint():
    tile = filament_tile("en", short=0, reach=[reach(1, 1, young=True), reach(2, None)])
    assert [r.text for r in tile.rows] == ["lasts 1 day", "no usage"]
    assert (tile.count, tile.tone, tile.young) == (0, "neutral", True)


def test_filament_stock_unknown():
    tile = filament_tile("en", short=None, reach=[])
    assert tile.count == "Stock unknown" and tile.rows == () and tile.tone == "neutral"


# ---- to do --------------------------------------------------------------------------------


def test_todo_always_three_lines_grey_at_zero():
    lines = todo_lines("en", checks=0, check_units=0, assembly_units=0, suggestions=0, suggestions_new=0, under_way=0)
    assert [(line.label, line.count, line.href, line.tone, line.note) for line in lines] == [
        ("To check", 0, "/inspection", "neutral", ""),
        ("Assembly", 0, "/assembly", "neutral", ""),
        ("Reorder suggestions", 0, "/reorder", "neutral", ""),
    ]


def test_todo_lines_name_pieces_new_and_under_way():
    lines = todo_lines("en", checks=2, check_units=3, assembly_units=4, suggestions=2, suggestions_new=1, under_way=1)
    assert [(line.count, line.tone, line.note) for line in lines] == [
        (2, "warn", "3 pieces"), (4, "warn", ""), (2, "warn", "1 new · 1 on the way"),
    ]


def test_german_texts():
    assert [p.text for p in orders_tile("de", open_count=4, ready=1, new=0).parts] == ["1 versandbereit", "3 warten auf Ware"]
    assert orders_tile("de", open_count=1, ready=0, new=0).parts[0].text == "1 wartet auf Ware"
    stock = stock_tile("de", ["ok", "ok", "low", "below_min", "overbooked"])
    assert stock.title == "Lager · 5 Artikel"
    assert [p.text for p in stock.parts] == ["2 reichen", "1 knapp (Nachdruck läuft)", "1 unter Minimum", "1 überbucht"]
    assert stock_tile("de", ["ok"]).parts[0].text == "1 reicht"
    fil = filament_tile("de", short=1, reach=[reach(1, 5, short=True, young=True), reach(2, 1)])
    assert [r.text for r in fil.rows] == ["reicht 5 Tage", "reicht 1 Tag"]
    assert fil.young_text == "Reichweite noch ungenau: weniger als 30 Tage Verbrauch bekannt"
    assert filament_tile("de", short=None, reach=[]).count == "Bestand unbekannt"
    lines = todo_lines("de", checks=1, check_units=1, assembly_units=0, suggestions=1, suggestions_new=1, under_way=2)
    assert [(line.label, line.note) for line in lines] == [
        ("Zu prüfen", "1 Stück"), ("Montage", ""), ("Bestellvorschläge", "1 neu · 2 unterwegs"),
    ]
