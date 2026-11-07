"""Colour distance for filament sorts (Spec 9.1): CIEDE2000 between two hex colours.

Migration 0006 imports `delta_e`, `similar` and `display_hex` from here: change their behaviour only
together with a new revision that keeps the old result for databases not yet migrated.

Pure functions, no I/O. Two spools whose colours are at most `SIMILAR` apart count as the same
colour. The value 7 is a decision of the project (two blacks #000000 / #1A1A1A: 5.5, together;
black / anthracite #333333: 13.4, apart). Alpha 00 means transparent: all transparent values are one colour,
never alike to an opaque one; a partly transparent value goes only with the same alpha.
"""

from __future__ import annotations

import math
import re

SIMILAR = 7.0

_HEX = re.compile(r"#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?")
# D65 reference white (sRGB)
_WHITE = (95.047, 100.0, 108.883)


def parse_rgba(value: str | None) -> tuple[int, int, int, int] | None:
    """RGB and alpha of `#RRGGBB[AA]` (the `#` is optional); alpha is 255 when the value has none.
    None if the value is no colour."""
    if value is None:
        return None
    match = _HEX.fullmatch(value.strip())
    if match is None:
        return None
    digits, alpha = match.group(1), match.group(2)
    return (
        int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16),
        255 if alpha is None else int(alpha, 16),
    )


def parse_hex(value: str | None) -> tuple[int, int, int] | None:
    """RGB of a colour value (see `parse_rgba`), the alpha part left out; None if it is no colour."""
    rgba = parse_rgba(value)
    return None if rgba is None else rgba[:3]


def is_transparent(value: str | None) -> bool:
    """Alpha 00 means transparent (Spec 9.1); its RGB part carries no meaning."""
    rgba = parse_rgba(value)
    return rgba is not None and rgba[3] == 0


def display_hex(value: str | None) -> str:
    """How a colour is shown: "transparent" for alpha 00, `#RRGGBB` for any other colour value `parse_rgba`
    understands (no alpha part, always a `#`), otherwise the text as it is, trimmed."""
    if is_transparent(value):
        return "transparent"
    rgb = parse_hex(value)
    if rgb is None:
        return (value or "").strip()
    return "#%02X%02X%02X" % rgb


def _linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (_linear(c) for c in rgb)
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) * 100.0
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) * 100.0
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) * 100.0

    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > 216.0 / 24389.0 else (24389.0 / 27.0 * t + 16.0) / 116.0

    fx, fy, fz = f(x / _WHITE[0]), f(y / _WHITE[1]), f(z / _WHITE[2])
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def delta_e2000_lab(
    lab1: tuple[float, float, float], lab2: tuple[float, float, float]
) -> float:
    """CIEDE2000 (Sharma, Wu, Dalal 2005) of two CIELAB colours; kL = kC = kH = 1."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    c_mean7 = ((c1 + c2) / 2.0) ** 7
    g = 0.5 * (1.0 - math.sqrt(c_mean7 / (c_mean7 + 25.0**7)))
    a1p, a2p = (1.0 + g) * a1, (1.0 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360.0 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360.0 if (a2p or b2) else 0.0

    dl = l2 - l1
    dc = c2p - c1p
    if c1p * c2p == 0.0:
        dh = 0.0
    else:
        diff = h2p - h1p
        if diff > 180.0:
            diff -= 360.0
        elif diff < -180.0:
            diff += 360.0
        dh = diff
    dhp = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh) / 2.0)

    l_mean = (l1 + l2) / 2.0
    c_mean = (c1p + c2p) / 2.0
    if c1p * c2p == 0.0:
        h_mean = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        h_mean = (h1p + h2p) / 2.0
    elif h1p + h2p < 360.0:
        h_mean = (h1p + h2p + 360.0) / 2.0
    else:
        h_mean = (h1p + h2p - 360.0) / 2.0

    t = (
        1.0
        - 0.17 * math.cos(math.radians(h_mean - 30.0))
        + 0.24 * math.cos(math.radians(2.0 * h_mean))
        + 0.32 * math.cos(math.radians(3.0 * h_mean + 6.0))
        - 0.20 * math.cos(math.radians(4.0 * h_mean - 63.0))
    )
    d_theta = 30.0 * math.exp(-(((h_mean - 275.0) / 25.0) ** 2))
    c_mean7p = c_mean**7
    rc = 2.0 * math.sqrt(c_mean7p / (c_mean7p + 25.0**7))
    sl = 1.0 + 0.015 * (l_mean - 50.0) ** 2 / math.sqrt(20.0 + (l_mean - 50.0) ** 2)
    sc = 1.0 + 0.045 * c_mean
    sh = 1.0 + 0.015 * c_mean * t
    rt = -math.sin(math.radians(2.0 * d_theta)) * rc

    return math.sqrt(
        (dl / sl) ** 2 + (dc / sc) ** 2 + (dhp / sh) ** 2 + rt * (dc / sc) * (dhp / sh)
    )


def delta_e(first: str | None, second: str | None) -> float | None:
    """CIEDE2000 between two hex colours; None when one of them is no colour."""
    rgb1, rgb2 = parse_hex(first), parse_hex(second)
    if rgb1 is None or rgb2 is None:
        return None
    return delta_e2000_lab(_lab(rgb1), _lab(rgb2))


def distance(first: str | None, second: str | None) -> float | None:
    """How far two colour values are apart for the grouping of Spec 9.1, None if they can never be one colour.

    Transparent values (alpha 00) are all alike, whatever their RGB (distance 0), and never alike to anything
    else. A partly transparent value goes only with the same alpha. Otherwise CIEDE2000 of the RGB parts.
    Two values that are no colour are alike (0) only when they are written alike; one that is none is never
    alike to a colour.
    """
    one, two = parse_rgba(first), parse_rgba(second)
    if one is None or two is None:
        if one is None and two is None:
            same = (first or "").strip().casefold() == (second or "").strip().casefold()
            return 0.0 if same else None
        return None
    if one[3] != two[3]:
        return None
    if one[3] == 0:
        return 0.0
    return delta_e2000_lab(_lab(one[:3]), _lab(two[:3]))


def similar(first: str | None, second: str | None) -> bool:
    """Spec 9.1: alike by `distance`, and at most `SIMILAR` apart."""
    d = distance(first, second)
    return d is not None and d <= SIMILAR
