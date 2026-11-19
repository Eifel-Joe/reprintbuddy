"""Whether the filament in a printer lasts for the prints of its lane, and when to reload (Spec 10.2
"Filament in den Druckern", p648). Only for display: nothing is booked, nothing changes in Bambuddy.

Per lane, in the order of its prints:
- **Need**: the grams per slot of the print file. The running print counts whole: Bambuddy takes the use
  off the spool only at the end (`main.py:9930`, after it reports the end at `:9501`), so the grams it
  holds still contain it. A print whose need is unknown is noted; reloads after it are "at the latest".
- **Slot**: as Bambuddy's `_match_filaments_to_slots` without the filament preset (`print_scheduler.py:5181-
  5421`): same material and same colour, else a similar colour (each channel within 40, `:5019-5035`),
  else the material alone; each slot once per print, in slot order. Bambuddy looks at the preset first
  when the print starts, so it may take another slot (p649 would follow it).
- **Stock**: the grams Bambuddy holds for the spool assigned to the slot. With the AMS filament backup on,
  every loaded slot of the same material and colour pools (as Bambuddy's own check at the start,
  `filament_deficit.py:876-908`); off or unknown, the slot alone. A pool with a slot of unknown grams is
  unknown: never a guess, never the AMS percentage.
- **Reload**: where the pool runs dry, linear over the print's whole time; already past: "now". Only the
  first place per pool; after it the pool counts as reloaded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from reprintbuddy.bambuddy.types import AmsSlot

SIMILAR_CHANNEL = 40  # Bambuddy's `_colors_are_similar`: each RGB channel at most this far apart
MAX_MARKERS = 3  # per lane; more are counted as "+ n" (Spec 10.2)

MarkerKind = Literal["reload", "not_loaded", "unknown_stock"]
SlotKey = tuple[int, int]


@dataclass(frozen=True)
class Need:
    material: str | None
    color: str | None  # "#RRGGBB"
    grams: float


@dataclass(frozen=True)
class LanePrint:
    """One print of a lane: its bar, the moment its whole print time began (for a running print: end less
    the file's print time), and its needs; None: not known."""

    start: datetime
    end: datetime
    print_start: datetime
    needs: tuple[Need, ...] | None


@dataclass(frozen=True)
class Marker:
    kind: MarkerKind
    material: str | None
    color: str | None
    slot: AmsSlot | None  # the slot chosen; None for "not loaded"
    at: datetime | None  # when to reload; None without a time
    due: bool = False  # the moment is already past: "reload now"
    latest: bool = False  # a print before it has an unknown need: "at the latest"


@dataclass(frozen=True)
class LaneFilament:
    markers: tuple[Marker, ...]
    more: int  # markers beyond MAX_MARKERS
    unknown_needs: tuple[int, ...]  # indexes of the prints whose need is unknown


def _material(value: str | None) -> str:
    return (value or "").strip().casefold()


def _rgb(color: str | None) -> tuple[int, int, int] | None:
    if not color or len(color) != 7:
        return None
    try:
        return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    except ValueError:
        return None


def _similar(a: str | None, b: str | None) -> bool:
    x, y = _rgb(a), _rgb(b)
    return x is not None and y is not None and all(abs(i - j) <= SIMILAR_CHANNEL for i, j in zip(x, y))


def _key(slot: AmsSlot) -> SlotKey:
    return slot.ams_id, slot.tray_id


def choose_slot(need: Need, loaded: Sequence[AmsSlot], used: set[SlotKey]) -> AmsSlot | None:
    """The slot Bambuddy would take for this need, without the filament preset (module text)."""
    free = [s for s in loaded if _key(s) not in used
            and (not need.material or _material(s.material) == _material(need.material))]
    color = (need.color or "").upper()
    for fits in (
        lambda s: color and (s.color or "").upper() == color,
        lambda s: color and _similar(s.color, color),
        lambda s: bool(need.material),
    ):
        found = next((s for s in free if fits(s)), None)
        if found is not None:
            return found
    return None


def lane_filament(
    prints: Sequence[LanePrint], slots: Sequence[AmsSlot], grams: Mapping[SlotKey, float],
    backup: bool | None, now: datetime,
) -> LaneFilament:
    """Walk the prints of one lane in order (see the module text). Pure: same input, same answer."""
    loaded = [s for s in slots if s.loaded]
    consumed: dict[tuple[SlotKey, ...], float] = {}
    done: set[tuple[SlotKey, ...]] = set()
    reloads: list[Marker] = []
    others: list[Marker] = []
    seen_missing: set[tuple[str, str]] = set()
    unknown_needs: list[int] = []
    unknown_before = False
    for index, job in enumerate(prints):
        if job.needs is None:
            unknown_needs.append(index)
            unknown_before = True
            continue
        used: set[SlotKey] = set()
        for need in job.needs:
            if not need.grams > 0:
                continue
            slot = choose_slot(need, loaded, used)
            if slot is None:
                sort = (_material(need.material), (need.color or "").upper())
                if sort not in seen_missing:
                    seen_missing.add(sort)
                    others.append(Marker("not_loaded", need.material, need.color, None, None))
                continue
            used.add(_key(slot))
            pool = (
                [s for s in loaded if _material(s.material) == _material(slot.material)
                 and (s.color or "").upper() == (slot.color or "").upper()]
                if backup is True else [slot]
            )
            pool_key = tuple(sorted(_key(s) for s in pool))
            if pool_key in done:
                continue
            if any(_key(s) not in grams for s in pool):
                done.add(pool_key)
                others.append(Marker("unknown_stock", slot.material, slot.color, slot, None))
                continue
            supply = sum(grams[_key(s)] for s in pool)
            before = consumed.get(pool_key, 0.0)
            consumed[pool_key] = before + need.grams
            if consumed[pool_key] <= supply:
                continue
            share = min(1.0, max(0.0, (supply - before) / need.grams))
            moment = job.print_start + (job.end - job.print_start) * share
            done.add(pool_key)
            reloads.append(Marker("reload", slot.material, slot.color, slot, moment,
                                  due=moment <= now, latest=unknown_before))
    ordered = sorted(reloads, key=lambda m: m.at or now) + others
    return LaneFilament(tuple(ordered[:MAX_MARKERS]), max(0, len(ordered) - MAX_MARKERS), tuple(unknown_needs))
