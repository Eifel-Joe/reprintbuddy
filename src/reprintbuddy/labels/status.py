"""What Brother's 32-byte status block says (Spec 8.5): the roll in the printer and its condition.

Before every print Reprintbuddy asks the printer. It does not print when the cover is open, the
roll is empty or missing, the cutter or the printer reports an error, or another roll than the
chosen one is in. A printer that is busy or about to switch itself off does not block (it keeps
jobs and prints them in turn). Layout from Brother's Raster Command Reference, checked against the
answer of a real QL-820NWB: the fixed bytes of the table differ between firmware versions, so only
the header (0x80, 0x20, "B", "4") is checked.

Byte 8 error 1: bit 0 no roll, bit 1 end of roll, bit 2 cutter jam, bit 4 busy, bit 5 switching off.
Byte 9 error 2: bit 0 replace roll, bit 1 buffer full, bit 2 transmission error, bit 4 cover open,
bit 6 roll cannot be fed (also the end of the roll), bit 7 system error.
Byte 10 width (mm), byte 11 type (0x0B / 0x4B die-cut, 0x4A continuous), byte 17 length (mm).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reprintbuddy.labels.rolls import CONTINUOUS_TYPES, DIE_CUT_TYPES, Roll, roll_in_printer

BLOCK_SIZE = 32

# error 1
_NO_ROLL, _END_OF_ROLL, _CUTTER = 0x01, 0x02, 0x04
# error 2
_REPLACE_ROLL, _BUFFER_FULL, _TRANSMISSION, _COVER_OPEN, _CANNOT_FEED, _SYSTEM = 0x01, 0x02, 0x04, 0x10, 0x40, 0x80


@dataclass(frozen=True)
class PrinterStatus:
    error1: int
    error2: int
    width_mm: int
    media_type: int
    length_mm: int


@dataclass(frozen=True)
class Problem:
    """Why nothing is printed: a key (`labels.problem.<key>`) and its parameters."""

    key: str
    params: dict[str, object] = field(default_factory=dict)


def parse(block: bytes | None) -> PrinterStatus | None:
    """The status, or None when the bytes are not a Brother status block."""
    if not isinstance(block, (bytes, bytearray)) or len(block) != BLOCK_SIZE:
        return None
    if tuple(block[:4]) != (0x80, 0x20, ord("B"), ord("4")):
        return None
    return PrinterStatus(block[8], block[9], block[10], block[11], block[17])


def problem(state: PrinterStatus, chosen: Roll) -> Problem | None:
    """The first thing that keeps the printer from printing the chosen roll, or None."""
    if state.error2 & _COVER_OPEN:
        return Problem("cover_open")
    if state.error1 & _END_OF_ROLL or state.error2 & (_CANNOT_FEED | _REPLACE_ROLL):
        return Problem("roll_empty")
    if state.error1 & _NO_ROLL or state.width_mm == 0:
        return Problem("no_roll")
    if state.error1 & _CUTTER:
        return Problem("cutter")
    if state.error2 & (_SYSTEM | _TRANSMISSION | _BUFFER_FULL):
        return Problem("error")
    continuous = state.media_type in CONTINUOUS_TYPES
    matches = (
        not continuous
        and state.media_type in DIE_CUT_TYPES
        and (state.width_mm, state.length_mm) == (chosen.width_mm, chosen.length_mm)
    )
    if matches:
        return None
    known = roll_in_printer(state.media_type, state.width_mm, state.length_mm)
    return Problem(
        "wrong_roll",
        {
            "width": state.width_mm,
            "length": state.length_mm,
            "continuous": continuous,
            "known": known.key if known is not None and known.key != chosen.key else None,
        },
    )
