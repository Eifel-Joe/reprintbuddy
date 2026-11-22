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
  unknown, named by that slot: never a guess, never the AMS percentage.
- **Slots one after the other** (p716): the slots of a pool run empty in turn, lowest stock first when
  Bambuddy's "lowest spool first" is on (external spools last), else in slot order; a slot of 0 g is no
  step (`_drain_order`). Each slot that runs empty before the last one gets a calm note "slot m empty,
  continuing with slot k", except inside a print that feeds two needs from the pool at once.
- **Reload**: where the pool runs dry, linear over the print's whole time; already past: "now". Only the
  first place per pool; after it the pool counts as reloaded. It names the slot in use then, the last one.
- **Reserve** (p720): with a reserve, a pool counts as dry once its stock falls to the reserve; only the reload
  moves, the slot notes still run to 0 g. Stock already at or below the reserve: the start of the print.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from reprintbuddy.bambuddy.types import AmsSlot

SIMILAR_CHANNEL = 40  # Bambuddy's `_colors_are_similar`: each RGB channel at most this far apart
EXTERNAL_AMS_ID = 255  # how Bambuddy stores an external spool's slot (`utils/tray_ids.py:55`, `parse._slot`)
MAX_MARKERS = 3  # per lane; more are counted as "+ n" (Spec 10.2)

MarkerKind = Literal["reload", "slot_empty", "not_loaded", "unknown_stock"]
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
    slot: AmsSlot | None  # the slot chosen, for "slot_empty" the one that runs empty; None for "not loaded"
    at: datetime | None  # when to reload or when the slot runs empty; None without a time
    due: bool = False  # the moment is already past: "reload now"
    latest: bool = False  # a print before it has an unknown need: "at the latest"
    next_slot: AmsSlot | None = None  # "slot_empty": the slot the printer goes on with (p716)


@dataclass(frozen=True)
class LaneFilament:
    markers: tuple[Marker, ...]
    more: int  # markers beyond MAX_MARKERS
    unknown_needs: tuple[int, ...]  # indexes of the prints whose need is unknown
    hidden_reloads: tuple[Marker, ...] = ()  # reloads beyond MAX_MARKERS: the head line still counts them


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


def _drain_order(pool: Sequence[AmsSlot], grams: Mapping[SlotKey, float], prefer_lowest: bool | None) -> list[AmsSlot]:
    """The order in which the slots of one pool run empty (Spec 10.2 "Vorrat", p716).

    Root: with the backup on, the plan summed the pool, so a slot that ran empty on the way went unseen until
    the whole sort was gone. Bambuddy picks the slot when a print starts: with "lowest spool first" the one
    with the fewest inventory grams, a tie by slot position (`print_scheduler.py:4761-4784`,
    `_prefer_lowest_sort_key`); otherwise the first in slot order. A slot run empty is no candidate any more,
    so the next start takes the next lowest (or the next in slot order). `pool` comes in slot order and the
    sort is stable, so a tie keeps it. Rejected: following the slot the printer's backup switches to in the
    middle of a print; Bambuddy does not report it, and the next start picks by the same rule anyway.
    """
    # Review p716, 5: a slot Bambuddy holds 0 g for is empty already; as a step it would get a note "empty" at the
    # start of the first print and be named as the slot the printer goes on with. Rejected: keeping it and
    # noting "empty now"; nothing runs empty there, the printer never takes it.
    with_filament = [s for s in pool if grams[_key(s)] > 0]
    if prefer_lowest is not True:
        return with_filament
    # Review p716, 3: Bambuddy ranks slots with inventory grams before all others and never looks up grams for an
    # external spool (`print_scheduler.py:5073`), so external spools come last, among themselves by the printer's
    # percentage, which Reprintbuddy does not use (Spec 10.2): there they keep their slot order.
    return sorted(with_filament, key=lambda s: (s.ams_id == EXTERNAL_AMS_ID,
                                                0.0 if s.ams_id == EXTERNAL_AMS_ID else grams[_key(s)]))


def _pool(slot: AmsSlot, loaded: Sequence[AmsSlot], backup: bool | None) -> list[AmsSlot]:
    """The slots whose stock counts for a need served from `slot`, in slot order (module text, "Stock")."""
    if backup is not True:
        return [slot]
    return [s for s in loaded if _material(s.material) == _material(slot.material)
            and (s.color or "").upper() == (slot.color or "").upper()]


def _pool_key(pool: Sequence[AmsSlot]) -> tuple[SlotKey, ...]:
    return tuple(sorted(_key(s) for s in pool))


def _moment(job: LanePrint, share: float) -> datetime:
    """Where in a print a share of its need is used up, linear over its whole print time."""
    return job.print_start + (job.end - job.print_start) * min(1.0, max(0.0, share))


def lane_filament(
    prints: Sequence[LanePrint], slots: Sequence[AmsSlot], grams: Mapping[SlotKey, float],
    backup: bool | None, now: datetime, *, prefer_lowest: bool | None = None, reserve: float = 0.0,
) -> LaneFilament:
    """Walk the prints of one lane in order (see the module text). Pure: same input, same answer.
    `prefer_lowest`: Bambuddy's "lowest spool first"; None not known (slot order). `reserve`: the grams per
    sort that count as used up already (Spec 10.2 "Reserve"); 0 with the switch off."""
    loaded = [s for s in slots if s.loaded]
    consumed: dict[tuple[SlotKey, ...], float] = {}
    done: set[tuple[SlotKey, ...]] = set()
    reloads: list[Marker] = []
    others: list[Marker] = []
    emptied: list[Marker] = []  # p716: slots of a pool that run empty before the last one
    seen_missing: set[tuple[str, str]] = set()
    reloaded: set[tuple[str, str]] = set()  # sorts with a reload note: each sort only once (Spec 10.2)
    unknown_needs: list[int] = []
    unknown_before = False
    for index, job in enumerate(prints):
        if job.needs is None:
            unknown_needs.append(index)
            unknown_before = True
            continue
        used: set[SlotKey] = set()
        chosen: list[tuple[Need, AmsSlot | None, list[AmsSlot]]] = []
        for need in job.needs:
            if not need.grams > 0:
                continue
            slot = choose_slot(need, loaded, used)
            if slot is not None:
                used.add(_key(slot))
            chosen.append((need, slot, _pool(slot, loaded, backup) if slot is not None else []))
        sharing = Counter(_pool_key(pool) for _need, slot, pool in chosen if slot is not None)
        for need, slot, pool in chosen:
            if slot is None:
                sort = (_material(need.material), (need.color or "").upper())
                if sort not in seen_missing:
                    seen_missing.add(sort)
                    others.append(Marker("not_loaded", need.material, need.color, None, None))
                continue
            pool_key = _pool_key(pool)
            if pool_key in done:
                continue
            missing = next((s for s in pool if _key(s) not in grams), None)
            if missing is not None:
                done.add(pool_key)
                # Review p716, 2: the note names the slot Bambuddy holds no grams for, where the spool is to be
                # assigned; with the backup off that is the chosen slot, as before.
                others.append(Marker("unknown_stock", slot.material, slot.color, missing, None))
                continue
            supply = sum(grams[_key(s)] for s in pool)
            before = consumed.get(pool_key, 0.0)
            after = before + need.grams
            consumed[pool_key] = after
            order = _drain_order(pool, grams, prefer_lowest)
            # Review p716, 4: two needs of one pool in one print take two slots (each slot once per print) and
            # print from both side by side, so none runs empty first: no slot note inside that print, the sum
            # still decides the reload. Rejected: draining them one after the other (a note for a slot that
            # does not run empty) and splitting the need per slot (Bambuddy reports neither the share nor
            # which slot the printer goes on with).
            steps = zip(order, order[1:]) if sharing[pool_key] == 1 else ()
            filled = 0.0
            for current, following in steps:
                filled += grams[_key(current)]
                # The slot runs empty inside this need when its end lies in [before, after): one used up exactly
                # by the need before is noted here, at the start of this print, where the printer goes on.
                if before <= filled < after:
                    moment = _moment(job, (filled - before) / need.grams)
                    emptied.append(Marker("slot_empty", current.material, current.color, current, moment,
                                          due=moment <= now, latest=unknown_before, next_slot=following))
            # p720: Bambuddy's grams are reckoned, not weighed (label weight, the file's estimate), so the sort
            # counts as used up at the reserve. Only here and at the moment below: the slot notes above stay at
            # 0 g, since the printer does go on with the next slot there. Rejected: lowering each slot's grams by
            # the reserve; that would move the slot notes and make a pool of small slots look empty.
            usable = supply - reserve
            if after <= usable:
                continue
            done.add(pool_key)
            sort = (_material(slot.material), (slot.color or "").upper())
            if sort in reloaded:  # a second slot of the same sort (backup off): already named
                continue
            reloaded.add(sort)
            moment = _moment(job, (usable - before) / need.grams)  # below the reserve already: the print's start
            # The slot in use when the sort runs dry; with no gram left anywhere, the slot chosen.
            reloads.append(Marker("reload", slot.material, slot.color, order[-1] if order else slot, moment,
                                  due=moment <= now, latest=unknown_before))
    return _limit(reloads, others, emptied, tuple(unknown_needs), now)


def _limit(
    reloads: list[Marker], others: list[Marker], emptied: list[Marker], unknown_needs: tuple[int, ...],
    now: datetime,
) -> LaneFilament:
    """At most MAX_MARKERS notes per lane, slot notes included (Spec 10.2, p716).

    Which ones: reloads first, then "not loaded" and "stock unknown", then the calm slot notes, so a slot note
    never pushes an orange reload into "+ n". Shown: those with a time in time order (a slot note before a
    reload of the same moment), then those without. Without slot notes this is the order before p716.
    Rejected: all notes in time order, cut after three; four slots of one sort would then hide the reload.
    """
    def by_time(m: Marker) -> tuple[datetime, bool]:
        return m.at or now, m.kind == "reload"

    ranked = sorted(reloads, key=by_time) + others + sorted(emptied, key=by_time)
    chosen = ranked[:MAX_MARKERS]
    shown = sorted((m for m in chosen if m.at is not None), key=by_time) + [m for m in chosen if m.at is None]
    hidden = tuple(m for m in ranked[MAX_MARKERS:] if m.kind == "reload")
    return LaneFilament(tuple(shown), max(0, len(ranked) - MAX_MARKERS), unknown_needs, hidden)
