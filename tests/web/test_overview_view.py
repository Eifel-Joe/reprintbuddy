"""The overview's numbers (Spec 10.2, "Was ansteht" since 0.4.1): the state of the Bambuddy connection
and the seven cards of what is due.

Pure: no app, no database. The route fills `Todo` from the same functions the pages behind the cards
use; this file checks what the cards make of it (order, numbers, small line, tone, where they lead).
"""

from dataclasses import replace

import pytest

from reprintbuddy.domain.engine import EngineStatus
from reprintbuddy.domain.status import article_status
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.overview_view import CONNECTION_KEYS, Todo, connection_state, red_counts, todo_cards


def row(name="A", *, available=5, min_stock=2, coverage=None):
    """A stand-in for a row of catalog.list_articles (one per variant) with the field the count reads."""
    from types import SimpleNamespace

    coverage = available if coverage is None else coverage
    return SimpleNamespace(name=name, status=article_status(available=available, min_stock=min_stock, coverage=coverage))


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


# ---- the cards of what is due -------------------------------------------------------------

EMPTY = Todo(
    orders_open=0, orders_new=0, orders_ready=0, checks=0, check_units=0, assembly_units=0,
    suggestions=0, suggestions_new=0, under_way=0, printing=0, waiting=0, filament_short=0,
    below_min=0, overbooked=0,
)


def en(key, **params):
    return translate("en", key, **params)


def card(cards, key, lang="en"):
    label = translate(lang, key)
    found = [c for c in cards if c.label == label]
    assert len(found) == 1, f"no card {key}"
    return found[0]


def note(c):
    """The small line as plain text and the links in it."""
    return " · ".join(part.text for part in c.note), [part.href for part in c.note if part.href]


def test_seven_cards_in_the_order_of_the_spec_grey_at_zero():
    cards = todo_cards("en", EMPTY)
    assert [c.label for c in cards] == [
        en("overview.todo.orders"), en("overview.todo.inspection"), en("overview.todo.assembly"),
        en("overview.todo.reorder"), en("overview.todo.prints"), en("overview.todo.filament"),
        en("overview.todo.below_min"),
    ]
    assert [c.value for c in cards] == [0] * 7
    assert [c.href for c in cards] == [
        "/orders", "/inspection", "/assembly", "/reorder", "/queue", "/filament", "/articles?status=below_min",
    ]
    assert [c.tone for c in cards] == ["neutral"] * 7  # a 0 stays, grey (Spec 10.2)
    assert [c.note for c in cards] == [()] * 7


def test_orders_new_and_ready_the_ready_ones_lead_to_their_filter():
    c = card(todo_cards("en", replace(EMPTY, orders_open=5, orders_new=2, orders_ready=1)), "overview.todo.orders")
    assert (c.value, c.tone, c.href) == (5, "warn", "/orders")
    assert note(c) == (
        f'{en("overview.todo.new", n=2)} · {en("overview.todo.orders_ready", n=1)}', ["/orders?status=ready"],
    )


def test_a_part_at_zero_is_left_out_of_the_small_line():
    c = card(todo_cards("en", replace(EMPTY, orders_open=3, orders_ready=2)), "overview.todo.orders")
    assert note(c) == (en("overview.todo.orders_ready", n=2), ["/orders?status=ready"])
    c = card(todo_cards("en", replace(EMPTY, orders_open=3, orders_new=3)), "overview.todo.orders")
    assert note(c) == (en("overview.todo.new", n=3), [])


def test_inspection_counts_prints_and_names_the_pieces():
    c = card(todo_cards("en", replace(EMPTY, checks=3, check_units=12)), "overview.todo.inspection")
    assert (c.value, c.tone) == (3, "warn")
    assert note(c) == (en("overview.todo.pieces", n=12), [])


def test_assembly_counts_pieces():
    c = card(todo_cards("en", replace(EMPTY, assembly_units=4)), "overview.todo.assembly")
    assert (c.value, c.tone, c.note) == (4, "warn", ())


def test_reorder_new_and_under_way():
    c = card(todo_cards("en", replace(EMPTY, suggestions=3, suggestions_new=1, under_way=2)), "overview.todo.reorder")
    assert (c.value, c.tone) == (3, "warn")
    assert note(c) == (f'{en("overview.todo.new", n=1)} · {en("overview.todo.under_way", n=2)}', [])
    # under way alone: nothing to order, the card is grey but says what is coming
    c = card(todo_cards("en", replace(EMPTY, under_way=2)), "overview.todo.reorder")
    assert (c.value, c.tone) == (0, "neutral")
    assert note(c) == (en("overview.todo.under_way", n=2), [])


def test_prints_running_and_waiting():
    c = card(todo_cards("en", replace(EMPTY, printing=1, waiting=4)), "overview.todo.prints")
    assert (c.value, c.tone, c.href) == (1, "warn", "/queue")
    assert note(c) == (en("overview.todo.waiting", n=4), [])


def test_filament_short_and_unknown():
    c = card(todo_cards("en", replace(EMPTY, filament_short=2)), "overview.todo.filament")
    assert (c.value, c.tone, c.href) == (2, "warn", "/filament")
    c = card(todo_cards("en", replace(EMPTY, filament_short=None)), "overview.todo.filament")
    assert (c.value, c.tone) == (en("overview.todo.stock_unknown"), "neutral")


def test_below_min_is_red_and_overbooked_leads_to_its_filter():
    c = card(todo_cards("en", replace(EMPTY, below_min=1, overbooked=2)), "overview.todo.below_min")
    assert (c.value, c.tone, c.href) == (3, "bad", "/articles?status=below_min")
    assert note(c) == (en("overview.todo.overbooked", n=2), ["/articles?status=overbooked"])


def test_red_counts_counts_rows_red_states_not_low():
    rows = [
        row("ok", available=5, min_stock=2),
        row("low", available=1, min_stock=2, coverage=4),  # yellow: the reprint covers it
        row("below", available=1, min_stock=2, coverage=1),
        row("over", available=-1, min_stock=0),
        row("over2", available=-2, min_stock=3),
    ]
    assert [r.status for r in rows] == ["ok", "low", "below_min", "overbooked", "overbooked"]
    assert red_counts(rows) == (1, 2)


def test_german_labels():
    cards = todo_cards("de", replace(EMPTY, filament_short=None))
    assert [c.label for c in cards] == [
        "Bestellungen", "Zu prüfen", "Montage", "Bestellvorschläge", "Drucke", "Filament", "Unter Mindestbestand",
    ]
    assert card(cards, "overview.todo.filament", "de").value == "Bestand unbekannt"
