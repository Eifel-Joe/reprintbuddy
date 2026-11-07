"""CIEDE2000 and the colour similarity of filament sorts (Spec 9.1)."""

import pytest

from reprintbuddy.domain import color


@pytest.mark.parametrize(
    "lab1,lab2,expected",
    [  # pairs from the test data of Sharma, Wu, Dalal (2005), table 1
        ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
        ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
        ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
        ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
        ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
        ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
        ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
        ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
        ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
    ],
)
def test_ciede2000_reference_values(lab1, lab2, expected):
    assert color.delta_e2000_lab(lab1, lab2) == pytest.approx(expected, abs=1e-3)
    assert color.delta_e2000_lab(lab2, lab1) == pytest.approx(expected, abs=1e-3)


@pytest.mark.parametrize(
    "a,b,expected",
    [  # the examples of Spec 9.1
        ("#000000", "#1A1A1A", 5.5),
        ("#000000", "#333333", 13.4),
        ("#FF0000", "#B30000", 16.0),
    ],
)
def test_spec_examples(a, b, expected):
    assert color.delta_e(a, b) == pytest.approx(expected, abs=0.5)


def test_spec_examples_decide_as_the_spec_says():
    assert color.similar("#000000", "#1A1A1A")
    assert not color.similar("#000000", "#333333")
    assert not color.similar("#FF0000", "#B30000")


def test_identical_and_symmetric():
    assert color.delta_e("#123456", "#123456") == 0.0
    assert color.delta_e("#123456", "#654321") == pytest.approx(color.delta_e("#654321", "#123456"))


@pytest.mark.parametrize(
    "value,rgb",
    [("#000000", (0, 0, 0)), ("ff8000", (255, 128, 0)), ("#FF800080", (255, 128, 0)), (" #0a0B0c ", (10, 11, 12))],
)
def test_parse_hex(value, rgb):
    assert color.parse_hex(value) == rgb


@pytest.mark.parametrize("value", [None, "", "black", "#12345", "#1234567", "#GGGGGG", "#12345678A"])
def test_parse_hex_rejects(value):
    assert color.parse_hex(value) is None


def test_alpha_is_ignored():
    assert color.delta_e("#000000FF", "#000000") == 0.0


def test_values_without_a_colour_are_alike_only_when_written_alike():
    assert color.similar(None, None) and color.similar("", None) and color.similar("Natural", " natural ")
    assert not color.similar("Natural", "Clear")
    assert not color.similar(None, "#000000")
    assert color.delta_e(None, "#000000") is None


@pytest.mark.parametrize(
    "value,shown",
    [("#000000", "#000000"), ("000000", "#000000"), ("00000000", "#000000"), ("#aabbccdd", "#AABBCC"),
     (" Natural ", "Natural"), (None, ""), ("", "")],
)
def test_display_hex_never_shows_the_alpha_part(value, shown):
    assert color.display_hex(value) == shown
