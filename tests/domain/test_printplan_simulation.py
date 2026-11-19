"""The print plan as a simulation in time (p646) lays Bambuddy's queue out exactly as the list rule before it.

Until 1.2.0 every entry, in the order of the queue, took the fitting printer that was free first. To put
prints in that reach Bambuddy only later (Reprintbuddy's own queue), the plan now walks the time: the
printer free first takes the first fitting entry. Both are list scheduling; this test holds them equal
on many random cases, with `_reference_plan` the loop of `build_plan` as of 1ac271d.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import datetime, timedelta

import pytest

from reprintbuddy.bambuddy.types import PrinterStatus
from reprintbuddy.domain import printplan as pp
from reprintbuddy.domain.printplan import Bar, Lane, Plan, PlanItem, PlanPrinter, build_plan

NOW = datetime(2026, 11, 19, 10, 0)


def _reference_plan(
    printers: Sequence[PlanPrinter], items: Sequence[PlanItem], now: datetime, *, shortest_first: bool = False
) -> Plan:
    """`build_plan` of 1ac271d, unchanged but for the module prefix."""
    conditions = {p.printer_id: pp._condition(p) for p in printers}
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
        if status is None:
            continue
        if status.state not in pp.PRINTING_STATES:
            continue
        entry = printing_items.get(pid)
        rest: timedelta | None = None
        if status.remaining_min:
            rest = timedelta(minutes=status.remaining_min)
        elif entry is not None and entry.duration is not None:
            rest = entry.duration * max(0.0, 1 - (status.progress or 0.0) / 100)
        known = rest is not None
        if rest is None:
            rest = pp.UNKNOWN_DURATION
            minimum = True
        label, own = (entry.label, entry.own) if entry is not None else (status.subtask_name or "", False)
        bar = Bar("running", label, own, now, now + rest, progress=status.progress,
                  paused=status.state == "PAUSE", duration_known=known)
        bars[pid].append(bar)
        free_at[pid] = bar.end + pp.GAP

    unplaced = manual = 0
    known_ids = set(conditions)
    for entry in pp._order([i for i in items if i.status == "pending"], shortest_first):
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
                if p.printer_id in in_service and pp.same_model(entry.target_model, p.model)
                and (not entry.target_location or entry.target_location == p.location)
                and pp.nozzle_fits(entry.nozzle, pp._nozzles(p))
            ]
            if not candidates:
                unplaced += 1
                continue
            target = min(candidates, key=lambda pid: (free_at[pid], pid))
            kind = "planned"
        duration = entry.duration
        if duration is None:
            duration, minimum = pp.UNKNOWN_DURATION, True
        start = free_at[target]
        bars[target].append(Bar(kind, entry.label, entry.own, start, start + duration,
                                duration_known=entry.duration is not None))
        free_at[target] = start + duration + pp.GAP

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


# ---- random cases ---------------------------------------------------------------------------------

MODELS = ("A1", "P1S", "Bambu Lab A1")
NOZZLES = (None, (0.4,), (0.6,), (0.4, 0.6))


def _status(rng: random.Random, pid: int) -> PrinterStatus | None:
    kind = rng.choice(["idle", "idle", "running", "running_no_rest", "pause", "offline", "none", "error", "plate"])
    if kind == "none":
        return None
    state = {"running": "RUNNING", "running_no_rest": "RUNNING", "pause": "PAUSE"}.get(kind, "IDLE")
    return PrinterStatus(
        pid, kind != "offline", state,
        progress=rng.choice([None, 10.0, 63.0]) if state != "IDLE" else None,
        remaining_min=rng.choice([20, 70, 300]) if kind in ("running", "pause") else None,
        subtask_name=rng.choice([None, "Other"]),
        awaiting_plate_clear=kind == "plate", stopped_error=kind == "error",
        nozzles=rng.choice(NOZZLES),
    )


def random_case(rng: random.Random):
    printers = [
        PlanPrinter(pid, f"Printer {pid}", rng.choice(MODELS), rng.choice([None, None, "Hall"]),
                    rng.random() > 0.15, _status(rng, pid))
        for pid in range(1, rng.randint(1, 4) + 1)
    ]
    items: list[PlanItem] = []
    for item_id in range(1, rng.randint(0, 12) + 1):
        fixed = rng.random() < 0.3
        printing = rng.random() < 0.15
        items.append(PlanItem(
            item_id=item_id, label=f"Item {item_id}", own=rng.random() < 0.5,
            status="printing" if printing else "pending",
            printer_id=rng.randint(1, 5) if fixed or printing else None,
            target_model=None if fixed else rng.choice(MODELS + ("X1C",)),
            target_location=rng.choice([None, None, None, "Hall", "Shed"]),
            duration=rng.choice([None, timedelta(minutes=10), timedelta(minutes=50), timedelta(hours=3),
                                 timedelta(minutes=50)]),
            manual_start=rng.random() < 0.08, starts_later=rng.random() < 0.08,
            been_jumped=rng.random() < 0.1, nozzle=rng.choice([None, 0.4, 0.6]),
        ))
    return printers, items, rng.random() < 0.4


@pytest.mark.parametrize("seed", range(500))
def test_simulation_matches_the_list_rule(seed):
    printers, items, shortest = random_case(random.Random(seed))
    assert build_plan(printers, items, NOW, shortest_first=shortest) == _reference_plan(
        printers, items, NOW, shortest_first=shortest
    )
