"""What an article can put on its label (Spec 8.5): every detail that can fit on a label.

The QR code is always on; these nine are chosen in the settings. Their order on the label is fixed
(the order below), whatever order they were ticked in. Text details stand alone, numbers get a short
word in front and stay in one row; the print target has its word too but is text: it breaks at blanks
like a free text instead of making all the letters smaller.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

FIELDS: tuple[str, ...] = (
    "name",
    "sku",
    "min_stock",
    "buffer",
    "target_qty",
    "units_per_print",
    "print_target",
    "label_text",
    "date",
)
DEFAULT_FIELDS: tuple[str, ...] = ("name", "sku", "label_text")  # Festlegung: what a label most often needs

_NUMBERS = ("min_stock", "buffer", "target_qty", "units_per_print")
_CAPTIONED = (*_NUMBERS, "print_target")  # these carry a short word, the rest is the bare value


@dataclass(frozen=True)
class LabelData:
    """The details of an article as they would be printed (the web layer fills this in)."""

    name: str
    sku: str | None
    min_stock: int
    buffer: int
    target_qty: int
    units_per_print: int
    print_target: str | None
    label_text: str | None
    date: str


@dataclass(frozen=True)
class Line:
    text: str
    bold: bool = False
    fixed: bool = False  # a number with its word ("Buffer: 2"): never broken into rows, the letters get smaller instead
    key: str = ""  # the detail this row belongs to (one of FIELDS), so a note can name it


def clean(chosen: object) -> tuple[str, ...]:
    """The known details of `chosen`, once each, in the fixed order; anything else is dropped."""
    if isinstance(chosen, (str, bytes)) or not isinstance(chosen, Iterable):
        return ()
    wanted = {item for item in chosen if isinstance(item, str)}
    return tuple(key for key in FIELDS if key in wanted)


def lines(data: LabelData, chosen: Iterable[str], caption: Callable[[str], str]) -> list[Line]:
    """The text lines for the chosen details; a detail without a value gives no line."""
    result: list[Line] = []
    for key in clean(list(chosen)):
        value = getattr(data, key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        text = " ".join(str(value).split())  # a line break or tab in a value is a blank: the font would draw rows of its own
        if key in _CAPTIONED:
            text = f"{caption(key)}: {text}"
        result.append(Line(text, bold=key == "name", fixed=key in _NUMBERS, key=key))
    return result


def sample(*, date: str, name: str) -> LabelData:
    """Made-up details for the trial print and the preview in the settings."""
    return LabelData(
        name=name, sku="ART-0001", min_stock=4, buffer=2, target_qty=6, units_per_print=2,
        print_target="Any A1", label_text="Shelf B3", date=date,
    )
