"""The cards of the key figures: the total always, one card per state with a count above zero."""

import pytest

from reprintbuddy.web.kpis import Kpi, state_cards


def t(key):
    return f"<{key}>"


def test_total_always_and_only_states_above_zero():
    cards = state_cards(t, "article", "package", 4, "Articles", [("overbooked", 1), ("low", 0)])
    assert cards == [
        Kpi(icon="package", tone="accent", value=4, label="Articles"),
        Kpi(icon="circle-alert", tone="bad", value=1, label="<status.article.overbooked>"),
    ]


def test_total_card_stays_at_zero():
    cards = state_cards(t, "part", "puzzle", 0, "Parts", [("below_min", 0)])
    assert [c.value for c in cards] == [0]
    assert cards[0].tone == "accent"


@pytest.mark.parametrize(
    ("kind", "state", "icon", "tone"),
    [
        ("article", "overbooked", "circle-alert", "bad"),
        ("article", "below_min", "triangle-alert", "bad"),
        ("set", "not_deliverable", "triangle-alert", "bad"),
        ("article", "low", "circle-alert", "warn"),
        ("order", "open", "circle-alert", "warn"),
        ("order", "ready", "check", "ok"),
        ("order", "shipped", "check", "neutral"),
        ("order", "cancelled", "x", "neutral"),
        ("article", "paused", "pause", "neutral"),
    ],
)
def test_icon_and_tone_per_state(kind, state, icon, tone):
    card = state_cards(t, kind, "package", 3, "All", [(state, 2)])[1]
    assert (card.icon, card.tone, card.value) == (icon, tone, 2)


def test_paused_uses_the_shared_label():
    card = state_cards(t, "article", "package", 3, "All", [("paused", 2)])[1]
    assert card.label == "<status.paused>"


def test_kpi_is_frozen():
    with pytest.raises(AttributeError):
        Kpi("x", "ok", 1, "l").value = 2
