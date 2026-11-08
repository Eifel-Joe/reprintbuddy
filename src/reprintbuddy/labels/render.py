"""Draws the label: the QR code of the article on the left, the chosen details on the right (Spec 8.5).

Always landscape, in the printable size of the roll (62 x 29 mm: 696 x 271 dots at 300 dpi). The
letters are as large as they can be so that every detail fits in height and width, never smaller
than `MIN_FONT_SIZE` (about 2 mm). A detail that is too long for one row takes a second one; one
that still does not fit is cut with an ellipsis and named in `truncated`, so the preview can say
what did not fit. Pure drawing: no network, no database.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import segno
from PIL import Image, ImageDraw, ImageFont

from reprintbuddy.labels import fields
from reprintbuddy.labels.rolls import Roll

FONT_DIR = Path(__file__).parent / "fonts"
FONT_REGULAR = FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"

MAX_FONT_SIZE = 64
MIN_FONT_SIZE = 24  # dots: about 2 mm at 300 dpi, still readable on a shelf
_QUIET_MODULES = 2  # white border around the code, in modules
_GAP = 14  # dots between the code and the text
_PAD_X = 6
_PAD_Y = 8
_ELLIPSIS = "…"
_MAX_ROWS = 2  # a detail that is too long for one row may take a second one
_ERROR_LEVEL = "m"
_CODE_SHARE = 0.46  # the code never takes more than this part of the width


@dataclass(frozen=True)
class Rendered:
    image: Image.Image  # 1 bit, the size of the roll
    lines: list[fields.Line]  # the rows drawn, top to bottom (a long detail takes two; a cut one ends in an ellipsis)
    truncated: list[int]  # indexes of the details (in the order chosen) that did not fit and were cut
    font_size: int
    code_width: int  # dots the code takes, with its quiet border


@lru_cache(maxsize=64)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)


def _draw_code(canvas: Image.Image, address: str, roll: Roll, centre: bool, has_text: bool) -> int:
    matrix = segno.make(address, error=_ERROR_LEVEL, micro=False).matrix
    across = len(matrix) + 2 * _QUIET_MODULES
    limit = min(roll.dots_high, int(roll.dots_wide * _CODE_SHARE)) if has_text else roll.dots_high
    module = max(1, limit // across)
    size = across * module
    left = (roll.dots_wide - size) // 2 if centre else 0
    top = (roll.dots_high - size) // 2
    draw = ImageDraw.Draw(canvas)
    origin = _QUIET_MODULES * module
    for row, cells in enumerate(matrix):
        for column, cell in enumerate(cells):
            if cell:
                x, y = left + origin + column * module, top + origin + row * module
                draw.rectangle([x, y, x + module - 1, y + module - 1], fill=0)
    return size


def _pitch(size: int) -> int:
    ascent, descent = _font(size, False).getmetrics()
    return ascent + descent


def _break_word(word: str, bold: bool, size: int, width: int) -> list[str]:
    """A word wider than a row, broken between letters."""
    font = _font(size, bold)
    pieces: list[str] = []
    current = ""
    for char in word:
        if current and font.getlength(current + char) > width:
            pieces.append(current)
            current = char
        else:
            current += char
    return [*pieces, current] if current else pieces


def _wrap(text: str, bold: bool, size: int, width: int) -> list[str]:
    """The text in rows no wider than `width`, broken at blanks (a word wider than a row on its own
    is broken between letters)."""
    font = _font(size, bold)
    rows: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if font.getlength(candidate) <= width:
            current = candidate
            continue
        if current:
            rows.append(current)
        if font.getlength(word) <= width:
            current = word
        else:
            *whole, current = _break_word(word, bold, size, width)
            rows.extend(whole)
    if current:
        rows.append(current)
    return rows or [""]


def _cut(text: str, bold: bool, size: int, width: int) -> str:
    """`text` shortened with an ellipsis so that it fits `width` (unchanged when it does)."""
    font = _font(size, bold)
    if font.getlength(text) <= width:
        return text
    low, high = 0, len(text)
    while low < high:  # the longest start of the text that still fits with the ellipsis
        middle = (low + high + 1) // 2
        if font.getlength(text[:middle].rstrip() + _ELLIPSIS) <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + _ELLIPSIS


def _arrange(
    lines: list[fields.Line], size: int, width: int, *, allow_cut: bool
) -> tuple[list[tuple[fields.Line, int]], list[int]] | None:
    """The rows of all details at `size`: a detail may take up to `_MAX_ROWS` rows. Too long for
    that: None, or with `allow_cut` its last row is cut with an ellipsis (and its index returned)."""
    rows: list[tuple[fields.Line, int]] = []
    cut: list[int] = []
    for index, line in enumerate(lines):
        parts = _wrap(line.text, line.bold, size, width)
        if len(parts) > _MAX_ROWS:
            if not allow_cut:
                return None
            tail = _cut(" ".join(parts[_MAX_ROWS - 1 :]), line.bold, size, width)
            parts = [*parts[: _MAX_ROWS - 1], tail]
            cut.append(index)
        rows.extend((fields.Line(part, line.bold), index) for part in parts)
    return rows, cut


def _choose(
    lines: list[fields.Line], width: int, height: int
) -> tuple[int, list[tuple[fields.Line, int]], list[int]]:
    """The largest size at which every detail fits whole; else the smallest size with what can be
    shown, cut where needed. Returns the size, the rows (with the index of their detail) and the
    indexes of the cut details."""
    for size in range(MAX_FONT_SIZE, MIN_FONT_SIZE - 1, -1):
        arranged = _arrange(lines, size, width, allow_cut=False)
        if arranged is not None and len(arranged[0]) * _pitch(size) <= height:
            return size, arranged[0], []
    size = MIN_FONT_SIZE
    arranged = _arrange(lines, size, width, allow_cut=True)
    if arranged is not None and len(arranged[0]) * _pitch(size) <= height:
        return size, arranged[0], arranged[1]
    # Too many rows even at the smallest size: one row per detail, each cut to the width.
    rows: list[tuple[fields.Line, int]] = []
    cut: list[int] = []
    for index, line in enumerate(lines):
        text = _cut(line.text, line.bold, size, width)
        if text != line.text:
            cut.append(index)
        rows.append((fields.Line(text, line.bold), index))
    return size, rows, cut


def render_label(
    data: fields.LabelData,
    chosen: Iterable[str],
    address: str,
    roll: Roll,
    caption: Callable[[str], str],
) -> Rendered:
    """The label picture for `data` with the chosen details and the code of `address`."""
    lines = fields.lines(data, chosen, caption)
    canvas = Image.new("L", (roll.dots_wide, roll.dots_high), 255)
    code_width = _draw_code(canvas, address, roll, centre=not lines, has_text=bool(lines))
    drawn: list[fields.Line] = []
    truncated: list[int] = []
    size = MIN_FONT_SIZE
    if lines:
        left = code_width + _GAP
        width = roll.dots_wide - left - _PAD_X
        height = roll.dots_high - 2 * _PAD_Y
        size, rows, truncated = _choose(lines, width, height)
        drawn = [row for row, _ in rows]
        pitch = _pitch(size)
        top = (roll.dots_high - len(rows) * pitch) // 2
        draw = ImageDraw.Draw(canvas)
        for position, line in enumerate(drawn):
            draw.text((left, top + position * pitch), line.text, font=_font(size, line.bold), fill=0, anchor="la")
    picture = canvas.point(lambda value: 255 if value >= 128 else 0).convert("1")
    return Rendered(picture, drawn, truncated, size, code_width)
