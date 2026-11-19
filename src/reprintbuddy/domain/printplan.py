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
- ... and whose nozzle fits the file (Spec 10.2 "Düse passt", p534): Bambuddy PR #3554 (commit d7e0c7e2d,
  `print_scheduler.py` at f23e80954: the pick `:3679`, the test `_nozzle_mismatch_message` `:1125-1151`).
  Unknown on either side fits. It holds whatever Bambuddy runs: an older one starts such an entry on the
  wrong printer and aborts it there at once (start guard, v1.3.1 `:9411-9428`, `nozzle_mismatch`), so drawing
  it on that printer would show a print that never runs. Entries pinned to a printer are left as they are.

Left out on purpose, so everything is "voraussichtlich": the filament check (`:3516-3660`) and the
printers' print hours (Bambuddy's tie-break, `_wear_order`). Among printers free at the same time the
lower id wins. Decisions of plan v0.4.2: a printer "im Fehler" is one that prints nothing and reports an
HMS message of severity 1 (F-a); models compare like Bambuddy's (F-c); an entry pinned to a printer that
Bambuddy does not list, or with no ready printer at all, is only counted (F-d, Review Focus 5); a print
running without a queue entry is shown under its subtask name as somebody else's (F-e).
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace as with_fields
from datetime import datetime, timedelta
from typing import Literal

from reprintbuddy.bambuddy.types import PrinterStatus

GAP = timedelta(minutes=5)  # between two prints of a printer, unlabelled (Spec 10.2)
UNKNOWN_DURATION = timedelta(hours=1)  # an entry without print time: an edge case (Spec 10.2)

PRINTING_STATES = frozenset({"RUNNING", "PREPARE", "SLICING", "PAUSE"})

Kind = Literal["running", "fixed", "planned", "backlog"]  # backlog: still waiting in Reprintbuddy (p646)
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
    nozzle: float | None = None  # mm the file was sliced for; None unknown
    # A print Reprintbuddy handed over and counts against its limit (an open `Dispatch`); its end frees a
    # place for the next print of Reprintbuddy's queue (p646). Not the same as `own`, which also takes
    # entries made in Bambuddy with the key's user.
    holds_slot: bool = False


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
    no_slot: int = 0  # prints of Reprintbuddy's queue that never get a place under the limit (p646)


def _model(value: str | None) -> str:
    return (value or "").replace("Bambu Lab ", "").strip().casefold()


def same_model(a: str | None, b: str | None) -> bool:
    """Bambuddy's comparison: "Bambu Lab " dropped, spaces around dropped, case ignored (F-c)."""
    return bool(_model(a)) and _model(a) == _model(b)


NOZZLE_TOLERANCE = 0.05  # mm; Bambuddy's, well inside the 0.2 between nozzle sizes (`:1151`)


def nozzle_fits(file_nozzle: float | None, printer_nozzles: Sequence[float] | None) -> bool:
    """Bambuddy's `_nozzle_mismatch_message` turned round: a printer is only ruled out on a positive
    mismatch, so an unknown nozzle of the file or of the printer fits; otherwise one of the printer's
    reachable nozzles (hotend or rack dock) must be within the tolerance."""
    if not file_nozzle or not printer_nozzles:
        return True
    return any(abs(d - file_nozzle) < NOZZLE_TOLERANCE for d in printer_nozzles)


def _nozzles(printer: PlanPrinter) -> tuple[float, ...] | None:
    return printer.status.nozzles if printer.status is not None else None


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


def _fits(entry: PlanItem, printer: PlanPrinter, in_service: set[int]) -> bool:
    """May this printer take the entry: its own printer, or one of the model (place, nozzle) in service."""
    if printer.printer_id not in in_service:
        return False
    if entry.printer_id is not None:
        return entry.printer_id == printer.printer_id
    return (
        same_model(entry.target_model, printer.model)
        and (not entry.target_location or entry.target_location == printer.location)
        and nozzle_fits(entry.nozzle, _nozzles(printer))
    )


def _order(items: Sequence[PlanItem], shortest_first: bool) -> list[PlanItem]:
    if not shortest_first:
        return list(items)
    index = {id(item): n for n, item in enumerate(items)}
    return sorted(
        items,
        key=lambda i: (not i.been_jumped, i.duration is None, i.duration or timedelta(0), index[id(i)]),
    )


def build_plan(
    printers: Sequence[PlanPrinter], items: Sequence[PlanItem], now: datetime, *, shortest_first: bool = False,
    backlog: Sequence[PlanItem] = (), free_slots: int = 0, insert_next: bool = False,
) -> Plan:
    """Lay the queue onto the printers (see the module text). Pure: same input, same plan.

    `backlog`: the prints of Reprintbuddy's own queue that the hand-over would take, top down, one item per
    print (Spec 10.2, p646). `free_slots`: the limit less Reprintbuddy's open prints (may be negative). A
    print is handed over while a place is free, else when a bar of an entry with `holds_slot` ends; then it
    goes to the end of Bambuddy's queue, or with `insert_next` behind the last waiting entry with
    `holds_slot` (at the top without one), as `dispatch.next_insert_position` does. The time to the next
    query is not counted (Spec 10.2). What never gets a place counts as `no_slot`."""
    conditions = {p.printer_id: _condition(p) for p in printers}
    in_service = {pid for pid, c in conditions.items() if c in ("ready", "plate")}
    bars: dict[int, list[Bar]] = {p.printer_id: [] for p in printers}
    waiting: dict[int, list[str]] = {p.printer_id: [] for p in printers}
    free_at: dict[int, datetime] = {}
    minimum = False
    openings: list[datetime] = []  # when places under the limit become free (ends of `holds_slot` bars)
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
        if entry is not None and entry.holds_slot:
            heapq.heappush(openings, bar.end)

    # Sorted out first, as Bambuddy's scheduler skips them or as they wait for one printer (module text).
    unplaced = manual = 0
    known_ids = set(conditions)

    def sort_out(entry: PlanItem) -> bool:
        """Counted or noted for a lane instead of laid out; True when a printer may take it."""
        nonlocal unplaced, manual
        if entry.manual_start or entry.starts_later:
            manual += 1
        elif entry.printer_id is not None and entry.printer_id not in known_ids:
            unplaced += 1
        elif entry.printer_id is not None and entry.printer_id not in in_service:
            waiting[entry.printer_id].append(entry.label)
        elif entry.printer_id is None and not any(_fits(entry, p, in_service) for p in printers):
            unplaced += 1
        else:
            return True
        return False

    # `order`: Bambuddy's queue as it waits, sorted-out entries too (they decide where "insert next" puts a
    # print); `takeable`: the entries a printer may take. Compared by identity: the prints of one entry of
    # Reprintbuddy's queue are equal as values.
    order = [i for i in items if i.status == "pending"]
    # In the order the scheduler walks them, so the texts "wartet: ..." keep that order.
    takeable = {id(e) for e in _order(order, shortest_first) if sort_out(e)}
    from_backlog: set[int] = set()
    backlog_left = list(backlog)
    slots = free_slots

    def hand_over() -> None:
        """Every print a free place allows goes into Bambuddy's queue, top down (Spec 7.3)."""
        nonlocal slots
        while backlog_left and slots > 0:
            slots -= 1
            entry = with_fields(backlog_left.pop(0), holds_slot=True)  # a new object: identity stays unique
            from_backlog.add(id(entry))
            if insert_next:
                own = [n for n, e in enumerate(order) if e.holds_slot]
                order.insert(own[-1] + 1 if own else 0, entry)
            else:
                order.append(entry)
            if sort_out(entry):  # one that is not laid out keeps its place: it would stay in Bambuddy
                takeable.add(id(entry))

    # p646 - root cause: the list rule ("each entry in turn takes the fitting printer free first") cannot
    # put in an entry that reaches Bambuddy's queue only later, after entries behind it have started.
    # Fix: walk the time. The printer free first that some waiting entry fits takes the first fitting
    # entry of the queue; equal times go to the lower id. Without late entries this is the same list
    # scheduling: the printer free first is also the first among the candidates of that entry, and the
    # entries before it fit no such printer (test_printplan_simulation.py holds both equal on random cases).
    # A hand-over at the same moment as a printer frees comes first: Reprintbuddy hands over in the round
    # of the query that sees the end (`engine._locked_tick`).
    # NOT-TO-DO: keeping the list rule and giving late entries a start "not before" their hand-over leaves
    # a printer idle that Bambuddy would fill with a later entry in the meantime.
    by_id = {p.printer_id: p for p in printers}
    clock = now
    hand_over()
    while True:
        ordered = _order([e for e in order if id(e) in takeable], shortest_first)
        pick: tuple[datetime, int, PlanItem] | None = None
        for pid in sorted(free_at, key=lambda pid: (max(free_at[pid], clock), pid)):
            fitting = next((e for e in ordered if _fits(e, by_id[pid], in_service)), None)
            if fitting is not None:
                pick = (max(free_at[pid], clock), pid, fitting)
                break
        if backlog_left and openings and (pick is None or openings[0] <= pick[0]):
            clock = max(clock, heapq.heappop(openings))
            slots += 1
            hand_over()
            continue
        if pick is None:  # nothing left a printer may take (every one left fits one: sorted out above)
            break
        start, target, entry = pick
        clock = start
        takeable.discard(id(entry))
        order[:] = [e for e in order if e is not entry]  # started: no longer waiting in Bambuddy
        duration = entry.duration
        if duration is None:
            duration, minimum = UNKNOWN_DURATION, True
        kind: Kind = "backlog" if id(entry) in from_backlog else "fixed" if entry.printer_id is not None else "planned"
        bars[target].append(Bar(kind, entry.label, entry.own, start, start + duration,
                                duration_known=entry.duration is not None))
        free_at[target] = start + duration + GAP
        if entry.holds_slot:
            heapq.heappush(openings, start + duration)

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
        no_slot=len(backlog_left),
    )
