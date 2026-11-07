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


# ---- filament page and reorder page (B6) -----------------------------------------------------


def test_filament_cards_total_stock_spools_and_unconfigured():
    from reprintbuddy.web.kpis import filament_cards

    cards = filament_cards(t, sorts=3, stock_g=4300, spools=7, unconfigured=2)
    assert cards == [
        Kpi(icon="disc-3", tone="accent", value=3, label="<kpi.filament_sorts>"),
        Kpi(icon="boxes", tone="info", value=4300, label="<kpi.filament_stock>"),
        Kpi(icon="package", tone="neutral", value=7, label="<kpi.filament_spools>"),
        Kpi(icon="triangle-alert", tone="warn", value=2, label="<kpi.filament_unconfigured>"),
    ]


def test_filament_cards_omit_unknown_stock_and_zero_unconfigured():
    from reprintbuddy.web.kpis import filament_cards

    cards = filament_cards(t, sorts=0, stock_g=None, spools=None, unconfigured=0)
    assert [c.value for c in cards] == [0]  # the total stays; unknown is not shown as 0


def test_reorder_cards_count_suggestions_and_what_is_on_its_way():
    from reprintbuddy.web.kpis import reorder_cards

    cards = reorder_cards(t, filament=2, parts=1, under_way=3)
    assert cards == [
        Kpi(icon="shopping-bag", tone="warn", value=3, label="<kpi.reorder_suggestions>"),
        Kpi(icon="disc-3", tone="warn", value=2, label="<nav.filament>"),
        Kpi(icon="puzzle", tone="warn", value=1, label="<kpi.parts>"),
        Kpi(icon="clock", tone="info", value=3, label="<reorder.under_way>"),
    ]


def test_reorder_cards_only_the_total_when_nothing_is_open():
    from reprintbuddy.web.kpis import reorder_cards

    cards = reorder_cards(t, filament=0, parts=0, under_way=0)
    assert [(c.value, c.tone) for c in cards] == [(0, "accent")]
