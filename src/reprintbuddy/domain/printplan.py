"""The print plan of the overview (Spec 10.2, 0.4.2): which printer prints what, and when - estimated.

Only for display. Reprintbuddy assigns nothing in Bambuddy (Spec 3.1); it lays Bambuddy's queue onto the
printers the way Bambuddy's scheduler would (`backend/app/services/print_scheduler.py`, 9712ab942):

- Entries in the order of the queue (position, then id; `:1754`). With Bambuddy's "shortest job first"
  (`queue_shortest_first`, `:1700-1739`): entries already jumped first, then the shorter print time,
  unknown print time last, the queue order breaks ties.
- Later start (`scheduled_time`) and manual start are skipped by the scheduler (`:2117`, `:2126`): here
  they are only counted.
- An entry pinned to a printer waits for that printer (`:2160 ff.`).
- An "any printer of the model" entry takes a free printer of that model that is not in maintenance,
  at its place if it names one (`:2416 ff.`, `:3295-3311`); no entry blocks another across printers.

Left out on purpose, so everything is "voraussichtlich": the filament check (`:3516-3660`) and the
printers' print hours (Bambuddy's tie-break, `_wear_order`). Among printers free at the same time the
lower id wins. Decisions of plan v0.4.2: a printer "im Fehler" is one that prints nothing and reports an
HMS message of severity 1 (F-a); models compare like Bambuddy's (F-c); an entry pinned to a printer that
Bambuddy does not list, or with no ready printer at all, is only counted (F-d, Review Focus 5); a print
running without a queue entry is shown under its subtask name as somebody else's (F-e).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from reprintbuddy.bambuddy.types import PrinterStatus

GAP = timedelta(minutes=5)  # between two prints of a printer, unlabelled (Spec 10.2)
UNKNOWN_DURATION = timedelta(hours=1)  # an entry without print time: an edge case (Spec 10.2)

PRINTING_STATES = frozenset({"RUNNING", "PREPARE", "SLICING", "PAUSE"})

Kind = Literal["running", "fixed", "planned"]
Condition = Literal["ready", "offline", "maintenance", "error", "plate"]


@dataclass(frozen=True)
class PlanPrinter:
    """A printer of Bambuddy's list with its state; `status` None: not reported (counts as offline)."""

    printer_id: int
    name: str
    model: str
    location: str | None
    active: bool
    status: PrinterStatus | None


@dataclass(frozen=True)
class PlanItem:
    """One entry of Bambuddy's queue, pending or printing, in the order of the queue."""

    item_id: int
    label: str
    own: bool
    status: str
    printer_id: int | None
    target_model: str | None
    target_location: str | None
    duration: timedelta | None
    manual_start: bool
    starts_later: bool
    been_jumped: bool = False


@dataclass(frozen=True)
class Bar:
    kind: Kind
    label: str
    own: bool
    start: datetime
    end: datetime
    progress: float | None = None
    paused: bool = False
    duration_known: bool = True


@dataclass(frozen=True)
class Lane:
    printer_id: int
    name: str
    condition: Condition
    bars: tuple[Bar, ...]
    waiting: tuple[str, ...]  # entries pinned to this printer while it is out of service


@dataclass(frozen=True)
class Plan:
    lanes: tuple[Lane, ...]
    finish: datetime | None  # end of the last bar; None without bars
    finish_is_minimum: bool  # a duration was unknown
    without: tuple[str, ...]  # printers out of service with entries waiting for them
    unplaced: int  # entries without a ready printer
    manual: int  # manual start or a later start


def _model(value: str | None) -> str:
    return (value or "").replace("Bambu Lab ", "").strip().casefold()


def same_model(a: str | None, b: str | None) -> bool:
    """Bambuddy's comparison: "Bambu Lab " dropped, spaces around dropped, case ignored (F-c)."""
    return bool(_model(a)) and _model(a) == _model(b)


def _condition(printer: PlanPrinter) -> Condition:
    status = printer.status
    if not printer.active:
        return "maintenance"
    if status is None or not status.connected:
        return "offline"
    printing = status.state in PRINTING_STATES
    if status.stopped_error and not printing:
        return "error"
    if status.awaiting_plate_clear and not printing:
        return "plate"
    return "ready"


def _order(items: Sequence[PlanItem], shortest_first: bool) -> list[PlanItem]:
    if not shortest_first:
        return list(items)
    index = {id(item): n for n, item in enumerate(items)}
    return sorted(
        items,
        key=lambda i: (not i.been_jumped, i.duration is None, i.duration or timedelta(0), index[id(i)]),
    )


def build_plan(
    printers: Sequence[PlanPrinter], items: Sequence[PlanItem], now: datetime, *, shortest_first: bool = False
) -> Plan:
    """Lay the queue onto the printers (see the module text). Pure: same input, same plan."""
    conditions = {p.printer_id: _condition(p) for p in printers}
    in_service = {pid for pid, c in conditions.items() if c in ("ready", "plate")}
    bars: dict[int, list[Bar]] = {p.printer_id: [] for p in printers}
    waiting: dict[int, list[str]] = {p.printer_id: [] for p in printers}
    free_at: dict[int, datetime] = {}
    minimum = False
    printing_items = {i.printer_id: i for i in items if i.status == "printing" and i.printer_id is not None}

    for printer in printers:
        pid = printer.printer_id
        if pid not in in_service:
            continue
        status = printer.status
        free_at[pid] = now
        if status is None:  # cannot be: in service means connected; kept explicit for the type checker
            continue
        if status.state not in PRINTING_STATES:
            continue
        entry = printing_items.get(pid)
        rest: timedelta | None = None
        if status.remaining_min:
            rest = timedelta(minutes=status.remaining_min)
        elif entry is not None and entry.duration is not None:
            rest = entry.duration * max(0.0, 1 - (status.progress or 0.0) / 100)
        known = rest is not None
        if rest is None:
            rest = UNKNOWN_DURATION
            minimum = True
        label, own = (entry.label, entry.own) if entry is not None else (status.subtask_name or "", False)
        bar = Bar("running", label, own, now, now + rest, progress=status.progress,
                  paused=status.state == "PAUSE", duration_known=known)
        bars[pid].append(bar)
        free_at[pid] = bar.end + GAP

    unplaced = manual = 0
    known_ids = set(conditions)
    for entry in _order([i for i in items if i.status == "pending"], shortest_first):
        if entry.manual_start or entry.starts_later:
            manual += 1
            continue
        if entry.printer_id is not None:
            if entry.printer_id not in known_ids:
                unplaced += 1
                continue
            if entry.printer_id not in in_service:
                waiting[entry.printer_id].append(entry.label)
                continue
            target, kind = entry.printer_id, "fixed"
        else:
            candidates = [
                p.printer_id for p in printers
                if p.printer_id in in_service and same_model(entry.target_model, p.model)
                and (not entry.target_location or entry.target_location == p.location)
            ]
            if not candidates:
                unplaced += 1
                continue
            target = min(candidates, key=lambda pid: (free_at[pid], pid))
            kind = "planned"
        duration = entry.duration
        if duration is None:
            duration, minimum = UNKNOWN_DURATION, True
        start = free_at[target]
        bars[target].append(Bar(kind, entry.label, entry.own, start, start + duration,
                                duration_known=entry.duration is not None))
        free_at[target] = start + duration + GAP

    lanes = tuple(
        Lane(p.printer_id, p.name, conditions[p.printer_id], tuple(bars[p.printer_id]), tuple(waiting[p.printer_id]))
        for p in printers
    )
    ends = [bar.end for lane in lanes for bar in lane.bars]
    return Plan(
        lanes=lanes,
        finish=max(ends) if ends else None,
        finish_is_minimum=minimum and bool(ends),
        without=tuple(lane.name for lane in lanes if lane.waiting),
        unplaced=unplaced,
        manual=manual,
    )
