"""The overview's numbers (Spec 10.2): the state of the Bambuddy connection and the four key figures.

Pure: no app, no database. The figures count the rows of `catalog.list_articles` and the prints of
the queue, so they follow the same rules as the article list, the queue and the reorder page.
"""

import pytest

from reprintbuddy.domain.engine import EngineStatus
from reprintbuddy.domain.status import article_status
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.overview_view import CONNECTION_KEYS, connection_state, figures


def row(name="A", *, available=5, min_stock=2, coverage=None, provisional=0, awaiting_assembly=0,
        in_progress_prints=0, paused=False):
    """A stand-in for catalog.ArticleRow with the fields the figures read."""
    from types import SimpleNamespace

    coverage = available if coverage is None else coverage
    return SimpleNamespace(
        name=name, available=available, min_stock=min_stock, provisional=provisional,
        awaiting_assembly=awaiting_assembly, in_progress_prints=in_progress_prints, paused=paused,
        status=article_status(available=available, min_stock=min_stock, coverage=coverage),
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


# ---- key figures --------------------------------------------------------------------------


def by_label(cards, key, lang="en"):
    label = translate(lang, key)
    found = [c for c in cards if c.label == label]
    assert len(found) == 1, f"no card {key}"
    return found[0]


def test_always_four_cards_in_the_order_of_the_spec_also_when_empty():
    cards = figures("en", [], queued_prints=0, open_prints=0, suggestions=0)
    assert [c.label for c in cards] == [
        translate("en", "overview.kpi.below_min"),
        translate("en", "overview.kpi.planned"),
        translate("en", "overview.kpi.to_check"),
        translate("en", "overview.kpi.reorder"),
    ]
    assert [c.value for c in cards] == [0, 0, 0, 0]
    assert [c.href for c in cards] == ["/articles", "/queue", "/inspection", "/reorder"]
    assert [c.note for c in cards] == ["", "", "", ""]
    # Nothing to do: green below the minimum, the others quiet.
    assert [c.tone for c in cards] == ["ok", "accent", "neutral", "neutral"]


def test_below_min_counts_the_red_states_not_low():
    rows = [
        row("ok", available=5, min_stock=2),
        row("low", available=1, min_stock=2, coverage=4),  # yellow: the reprint covers it
        row("below", available=1, min_stock=2, coverage=1),
        row("over", available=-1, min_stock=0),
        row("over2", available=-2, min_stock=3),
    ]
    assert [r.status for r in rows] == ["ok", "low", "below_min", "overbooked", "overbooked"]
    card = by_label(figures("en", rows, queued_prints=0, open_prints=0, suggestions=0), "overview.kpi.below_min")
    assert card.value == 3
    assert card.tone == "bad"
    assert card.note == translate("en", "overview.kpi.overbooked_note", n=2)


def test_planned_adds_the_queue_and_bambuddy():
    card = by_label(figures("en", [], queued_prints=4, open_prints=3, suggestions=0), "overview.kpi.planned")
    assert card.value == 7
    assert card.note == translate("en", "overview.kpi.planned_note", queued=4, open=3)


def test_to_check_adds_provisional_and_assembly_units():
    rows = [row("a", provisional=4), row("b", awaiting_assembly=2), row("c", provisional=1, awaiting_assembly=1)]
    card = by_label(figures("en", rows, queued_prints=0, open_prints=0, suggestions=0), "overview.kpi.to_check")
    assert card.value == 8
    assert card.tone == "warn"
    assert card.note == translate("en", "overview.kpi.to_check_note", check=5, assembly=3)


def test_reorder_shows_the_count_yellow():
    card = by_label(figures("en", [], queued_prints=0, open_prints=0, suggestions=2), "overview.kpi.reorder")
    assert card.value == 2
    assert card.tone == "warn"


def test_german_labels():
    cards = figures("de", [], queued_prints=0, open_prints=0, suggestions=0)
    assert [c.label for c in cards] == [
        "Unter Mindestbestand", "Geplant und in Arbeit", "Zu prüfen", "Bestellvorschläge",
    ]
