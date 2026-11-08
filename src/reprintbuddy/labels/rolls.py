"""The label rolls a printer can have in (Spec 8.5). For now only 62 x 29 mm, die-cut (DK-11209).

Printable size and margins are Brother's own numbers for the QL-810W / QL-820NWB at 300 dpi
(Raster Command Reference): the head has 720 dots, 12 of them as margin on each side of a
62 mm die-cut label, 696 print; 271 lines along the 29 mm. The picture is always landscape:
696 dots wide, 271 high.
"""

from __future__ import annotations

from dataclasses import dataclass

HEAD_DOTS = 720  # dots of the print head = 90 bytes per raster line

MEDIA_DIE_CUT = 0x0B  # what the printer reports for a die-cut roll (some firmware says 0x4B)
MEDIA_DIE_CUT_ALT = 0x4B
MEDIA_CONTINUOUS = 0x4A  # (older documentation: 0x0A)
MEDIA_CONTINUOUS_ALT = 0x0A
CONTINUOUS_TYPES = (MEDIA_CONTINUOUS, MEDIA_CONTINUOUS_ALT)
DIE_CUT_TYPES = (MEDIA_DIE_CUT, MEDIA_DIE_CUT_ALT)


@dataclass(frozen=True)
class Roll:
    key: str  # what is stored in the settings
    name: str  # the Brother name of the roll
    width_mm: int
    length_mm: int
    dots_wide: int  # printable dots across the tape (the picture's width)
    dots_high: int  # printable lines along the tape (the picture's height)
    margin_dots: int  # dots left unused on each side of the head
    media_type: int = MEDIA_DIE_CUT


DK_11209 = Roll("DK-11209", "DK-11209", 62, 29, 696, 271, 12)

ROLLS: dict[str, Roll] = {DK_11209.key: DK_11209}
DEFAULT_ROLL = DK_11209.key


def roll_by_key(key: object) -> Roll | None:
    return ROLLS.get(key) if isinstance(key, str) else None


def roll_in_printer(media_type: int, width_mm: int, length_mm: int) -> Roll | None:
    """The known roll that matches what the printer reports, or None."""
    for roll in ROLLS.values():
        if media_type in DIE_CUT_TYPES and (roll.width_mm, roll.length_mm) == (width_mm, length_mm):
            return roll
    return None
