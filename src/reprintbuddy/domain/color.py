"""Colour distance for filament sorts (Spec 9.1): CIEDE2000 between two hex colours.

Migration 0006 imports `delta_e`, `similar` and `display_hex` from here: change their behaviour only
together with a new revision that keeps the old result for databases not yet migrated.

Pure functions, no I/O. Two spools whose colours are at most `SIMILAR` apart count as the same
colour. The value 7 is a decision of the project (two blacks #000000 / #1A1A1A: 5.5, together;
black / anthracite #333333: 13.4, apart).
"""

from __future__ import annotations

import math
import re

SIMILAR = 7.0

_HEX = re.compile(r"#?([0-9A-Fa-f]{6})(?:[0-9A-Fa-f]{2})?")
# D65 reference white (sRGB)
_WHITE = (95.047, 100.0, 108.883)


def parse_hex(value: str | None) -> tuple[int, int, int] | None:
    """RGB of `#RRGGBB` (also `RRGGBB` and `#RRGGBBAA`, the alpha part is ignored); None if it is no colour."""
    if value is None:
        return None
    match = _HEX.fullmatch(value.strip())
    if match is None:
        return None
    digits = match.group(1)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def display_hex(value: str | None) -> str:
    """How a colour is shown: `#RRGGBB` for anything `parse_hex` understands (no alpha part, always a `#`),
    otherwise the text as it is, trimmed."""
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


def similar(first: str | None, second: str | None) -> bool:
    """Spec 9.1: at most `SIMILAR` apart. Two values that are no colour are alike only when they are written alike."""
    distance = delta_e(first, second)
    if distance is None:
        return parse_hex(first) is None and parse_hex(second) is None and (
            (first or "").strip().casefold() == (second or "").strip().casefold()
        )
    return distance <= SIMILAR
