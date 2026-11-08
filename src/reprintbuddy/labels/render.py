"""Draws the label: the QR code of the article on the left, the chosen details on the right (Spec 8.5).

Always landscape, in the printable size of the roll (62 x 29 mm: 696 x 271 dots at 300 dpi). The
letters are as large as they can be so that every detail fits in height and width, never smaller
than `MIN_FONT_SIZE` (about 2 mm). A number with its word ("Buffer: 2") always stays in one row, the
letters get smaller instead; a text (also the print target with its word) that is too long for one
row takes a second one, broken at blanks. Only at the smallest size is a word broken between letters, and a detail that still does not
fit is cut with an ellipsis and named in `truncated`, so the preview can say what did not fit. When even one
row per detail is too high for the roll (all nine details), the last details are left off, never drawn
over the margin, and named in `dropped`. Pure drawing: no network, no database.
"""

from __future__ import annotations

import unicodedata
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
_QUIET_MODULES = 3  # white border around the code, in modules (the paper's own margin adds to it)
_GAP = 14  # dots between the code and the text
_PAD_X = 6
_PAD_Y = 8
_ELLIPSIS = "…"
_MAX_ROWS = 2  # a detail that is too long for one row may take a second one
_ERROR_LEVEL = "m"
_CODE_SHARE = 0.46  # the code never takes more than this part of the width
# A code whose dots (modules) are smaller than this is flagged: 6 dots = 0.51 mm at 300 dpi, the usual
# advice for a phone held at arm's length is 0.5 mm. The code is at most 271 dots high: an address of
# up to 86 characters (code version 5) keeps 6 dots, the usual one of 30 characters has 7, one of 87 to
# 106 characters (version 6) has 5, one of about 125 has 4 (0.34 mm).
MIN_MODULE_DOTS = 6
_NOTDEF_PROBE = "￿"  # a noncharacter no font has: drawn as the font's own "missing" box


@dataclass(frozen=True)
class Rendered:
    image: Image.Image  # 1 bit, the size of the roll
    lines: list[fields.Line]  # the rows drawn, top to bottom (a long detail takes two; a cut one ends in an ellipsis)
    truncated: list[int]  # indexes of the details (in the order chosen) that did not fit and were cut
    font_size: int
    dropped: tuple[str, ...]  # keys of the details left off because there is no room in the height (the last ones)
    code_width: int  # dots the code takes, with its quiet border
    module_dots: int  # dots of one square of the code
    missing: tuple[str, ...] = ()  # characters of the rows drawn that the font does not have (they show as an empty box)
    rtl: bool = False  # the rows hold letters that run right to left, which are drawn left to right

    @property
    def fine_code(self) -> bool:
        """The code is so fine that a phone may read it badly (a very long address)."""
        return self.module_dots < MIN_MODULE_DOTS


@lru_cache(maxsize=64)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    # The basic layout on every machine: raqm (where a system has it) would draw right-to-left text the
    # right way round and measure other widths, so the picture and the note about it would differ.
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size, layout_engine=ImageFont.Layout.BASIC)


def _draw_code(canvas: Image.Image, address: str, roll: Roll, centre: bool, has_text: bool) -> tuple[int, int]:
    """Draws the code; returns its width with the quiet border and the dots of one square."""
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
    return size, module


def _pitch(size: int) -> int:
    ascent, descent = _font(size, False).getmetrics()
    return ascent + descent


@lru_cache(maxsize=1024)
def _has_glyph(char: str, bold: bool) -> bool:
    font = _font(40, bold)
    return bytes(font.getmask(char)) != bytes(font.getmask(_NOTDEF_PROBE))


def _missing(rows: Iterable[fields.Line]) -> tuple[str, ...]:
    """The characters of `rows` the font has no glyph for, once each, in the order they appear."""
    found: dict[str, None] = {}
    for row in rows:
        for char in row.text:
            if unicodedata.category(char)[0] in "CZ" or char in found:
                continue
            if not _has_glyph(char, row.bold):
                found[char] = None
    return tuple(found)


def _runs_right_to_left(rows: Iterable[fields.Line]) -> bool:
    return any(unicodedata.bidirectional(char) in ("R", "AL") for row in rows for char in row.text)


def _break_word(word: str, bold: bool, size: int, width: int) -> list[str]:
    """A word wider than a row, broken between letters (the very last resort)."""
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


def _wrap(text: str, bold: bool, size: int, width: int, *, split_words: bool) -> list[str] | None:
    """The text in rows no wider than `width`, broken at blanks only. A word wider than a row on its
    own: None, or with `split_words` broken between letters."""
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
        elif not split_words:
            return None
        else:
            *whole, current = _break_word(word, bold, size, width)
            rows.extend(whole)
    if current:
        rows.append(current)
    return rows or [""]


def _cut(text: str, bold: bool, size: int, width: int) -> str:
    """`text` shortened with an ellipsis so that it fits `width` (unchanged when it does). The start
    stays, so the word before a number is kept as long as it fits."""
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
    lines: list[fields.Line], size: int, width: int, *, split_words: bool, allow_cut: bool
) -> tuple[list[tuple[fields.Line, int]], list[int]] | None:
    """The rows of all details at `size`, or None when they do not fit the width:
    - a number with its word in front ("Buffer: 2") is one row and never broken;
    - a text (the print target with its word too) is one row, else broken at blanks into up to
      `_MAX_ROWS` rows (a word wider than a row is broken between letters only with `split_words`);
    - what still does not fit is cut with an ellipsis (the index is returned) if `allow_cut`, else None."""
    rows: list[tuple[fields.Line, int]] = []
    cut: list[int] = []
    for index, line in enumerate(lines):
        if line.fixed:
            text = _cut(line.text, line.bold, size, width)
            if text != line.text:
                if not allow_cut:
                    return None
                cut.append(index)
            rows.append((fields.Line(text, line.bold), index))
            continue
        parts = _wrap(line.text, line.bold, size, width, split_words=split_words)
        if parts is None:  # a word wider than a row, and breaking it is not allowed at this size
            return None
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
) -> tuple[int, list[tuple[fields.Line, int]], list[int], tuple[str, ...]]:
    """The largest size at which every detail fits whole, without a word broken between letters; at
    the smallest size, in this order: words broken between letters, then details cut with an
    ellipsis, then one row per detail, and if that is still too high the last details are left off.
    Returns the size, the rows (with the index of their detail), the indexes of the cut details and
    the keys of the details left off."""
    for size in range(MAX_FONT_SIZE, MIN_FONT_SIZE - 1, -1):
        arranged = _arrange(lines, size, width, split_words=False, allow_cut=False)
        if arranged is not None and len(arranged[0]) * _pitch(size) <= height:
            return size, arranged[0], [], ()
    size = MIN_FONT_SIZE
    for allow_cut in (False, True):
        arranged = _arrange(lines, size, width, split_words=True, allow_cut=allow_cut)
        if arranged is not None and len(arranged[0]) * _pitch(size) <= height:
            return size, arranged[0], arranged[1], ()
    # Too many rows even at the smallest size: one row per detail, each cut to the width.
    rows: list[tuple[fields.Line, int]] = []
    cut: list[int] = []
    for index, line in enumerate(lines):
        text = _cut(line.text, line.bold, size, width)
        if text != line.text:
            cut.append(index)
        rows.append((fields.Line(text, line.bold), index))
    room = max(1, height // _pitch(size))  # rows that fit between the margins (one at least: never an empty text area)
    dropped = tuple(line.key for line in lines[room:])
    cut = [index for index in cut if index < room]
    return size, rows[:room], cut, dropped


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
    code_width, module = _draw_code(canvas, address, roll, centre=not lines, has_text=bool(lines))
    drawn: list[fields.Line] = []
    truncated: list[int] = []
    dropped: tuple[str, ...] = ()
    size = MIN_FONT_SIZE
    if lines:
        left = code_width + _GAP
        width = roll.dots_wide - left - _PAD_X
        height = roll.dots_high - 2 * _PAD_Y
        size, rows, truncated, dropped = _choose(lines, width, height)
        drawn = [row for row, _ in rows]
        pitch = _pitch(size)
        top = (roll.dots_high - len(rows) * pitch) // 2
        draw = ImageDraw.Draw(canvas)
        for position, line in enumerate(drawn):
            draw.text((left, top + position * pitch), line.text, font=_font(size, line.bold), fill=0, anchor="la")
    picture = canvas.point(lambda value: 255 if value >= 128 else 0).convert("1")
    return Rendered(picture, drawn, truncated, size, dropped, code_width, module, _missing(drawn), _runs_right_to_left(drawn))
