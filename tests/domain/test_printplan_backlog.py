"""Prints still waiting in Reprintbuddy's own queue in the print plan (Spec 10.2, p646).

A print goes to Bambuddy as soon as fewer of Reprintbuddy's prints are open there than the limit: at once
while there is room, else when an open print of Reprintbuddy ends in the plan. Then it stands where
Reprintbuddy puts it (at the end, or with "insert next" behind its own waiting prints) and is laid out like
any entry. The time to the next query is not counted.
"""

import random
from datetime import datetime, timedelta

import pytest

from reprintbuddy.bambuddy.types import PrinterStatus
from reprintbuddy.domain.printplan import GAP, PlanItem, PlanPrinter, build_plan

from test_printplan_simulation import random_case

NOW = datetime(2026, 11, 19, 10, 0)


def at(hour: int, minute: int = 0) -> datetime:
    return NOW.replace(hour=hour, minute=minute)


def status(pid: int, state="IDLE", **kw) -> PrinterStatus:
    values = dict(connected=True, state=state, progress=None, remaining_min=None, subtask_name=None,
                  awaiting_plate_clear=False, stopped_error=False)
    values.update(kw)
    return PrinterStatus(pid, **values)


def printer(pid: int, *, model="A1", st: PrinterStatus | None | str = "idle", active=True) -> PlanPrinter:
    return PlanPrinter(pid, f"Printer {pid}", model, None, active, status(pid) if st == "idle" else st)


_ids = iter(range(1, 10_000))


def item(label="Other", *, minutes: int | None = 60, own=False, holds_slot=False, printer_id=None, model="A1",
         state="pending", manual=False, nozzle=None) -> PlanItem:
    return PlanItem(
        item_id=next(_ids), label=label, own=own or holds_slot, status=state, printer_id=printer_id,
        target_model=None if printer_id else model, target_location=None,
        duration=None if minutes is None else timedelta(minutes=minutes),
        manual_start=manual, starts_later=False, nozzle=nozzle, holds_slot=holds_slot,
    )


def waiting(label="Clip", *, minutes: int | None = 30, printer_id=None, model="A1", nozzle=None) -> PlanItem:
    """A print of Reprintbuddy's own queue: no Bambuddy id yet (0)."""
    return PlanItem(
        item_id=0, label=label, own=True, status="pending", printer_id=printer_id,
        target_model=None if printer_id else model, target_location=None,
        duration=None if minutes is None else timedelta(minutes=minutes),
        manual_start=False, starts_later=False, nozzle=nozzle,
    )


def bars(plan, pid):
    lane = next(lane for lane in plan.lanes if lane.printer_id == pid)
    return [(b.kind, b.label, b.start, b.end) for b in lane.bars]


def running(pid: int, minutes: int) -> PlanPrinter:
    return printer(pid, st=status(pid, "RUNNING", progress=50.0, remaining_min=minutes))


@pytest.mark.parametrize("seed", range(200))
@pytest.mark.parametrize("slots, insert_next", [(0, False), (3, True), (-2, False)])
def test_empty_backlog_changes_nothing(seed, slots, insert_next):
    printers, items, shortest = random_case(random.Random(seed))
    assert build_plan(printers, items, NOW, shortest_first=shortest, backlog=(), free_slots=slots,
                      insert_next=insert_next) == build_plan(printers, items, NOW, shortest_first=shortest)


def test_backlog_with_free_slot_starts_on_the_free_printer_now():
    plan = build_plan([printer(1)], [], NOW, backlog=[waiting()], free_slots=1)
    assert bars(plan, 1) == [("backlog", "Clip", NOW, at(10, 30))]
    assert plan.finish == at(10, 30) and plan.no_slot == 0


def test_backlog_waits_for_an_own_print_to_end_when_the_limit_is_reached():
    """Limit full: the own print on printer 1 ends 11:00, the print is handed over then and goes to the
    printer free first, printer 2 (busy until 10:40 + gap)."""
    own = item("Halter", state="printing", printer_id=1, holds_slot=True)
    plan = build_plan([running(1, 60), running(2, 40)], [own], NOW, backlog=[waiting()], free_slots=0)
    assert bars(plan, 2)[-1] == ("backlog", "Clip", at(11), at(11, 30))


def test_foreign_print_ending_frees_no_slot():
    foreign = item("Fremd", state="printing", printer_id=1)
    plan = build_plan([running(1, 60)], [foreign], NOW, backlog=[waiting()], free_slots=0)
    assert bars(plan, 1) == [("running", "Fremd", NOW, at(11))]
    assert plan.no_slot == 1 and plan.finish == at(11)


def test_own_by_user_name_but_not_handed_over_by_reprintbuddy_frees_no_slot():
    """`own` (made with the key's user) is not `holds_slot` (a print Reprintbuddy handed over and counts)."""
    by_user = item("Halter", state="printing", printer_id=1, own=True)
    plan = build_plan([running(1, 60)], [by_user], NOW, backlog=[waiting()], free_slots=0)
    assert plan.no_slot == 1


def test_insert_next_off_appends_behind_foreign_entries():
    foreign = item("Fremd", minutes=60)
    own = item("Halter", state="printing", printer_id=1, holds_slot=True)
    plan = build_plan([running(1, 30)], [own, foreign], NOW, backlog=[waiting()], free_slots=0,
                      insert_next=False)
    assert [b[1] for b in bars(plan, 1)] == ["Halter", "Fremd", "Clip"]


def test_insert_next_on_goes_before_foreign_entries_not_yet_started():
    """Handed over at 10:30, when the foreign entry has not started (printer busy until 10:35)."""
    foreign = item("Fremd", minutes=60)
    own = item("Halter", state="printing", printer_id=1, holds_slot=True)
    plan = build_plan([running(1, 30)], [own, foreign], NOW, backlog=[waiting()], free_slots=0,
                      insert_next=True)
    assert bars(plan, 1)[1:] == [("backlog", "Clip", at(10, 35), at(11, 5)),
                                 ("planned", "Fremd", at(11, 10), at(12, 10))]


def test_insert_next_on_does_not_overtake_a_foreign_entry_already_started():
    """Printer 2 is idle: the foreign entry starts at once, before the hand-over at 10:30."""
    foreign = item("Fremd", minutes=60)
    own = item("Halter", state="printing", printer_id=1, holds_slot=True)
    plan = build_plan([running(1, 30), printer(2)], [own, foreign], NOW, backlog=[waiting()], free_slots=0,
                      insert_next=True)
    assert bars(plan, 2) == [("planned", "Fremd", NOW, at(11))]
    assert bars(plan, 1)[1] == ("backlog", "Clip", at(10, 35), at(11, 5))


def test_insert_next_on_stays_behind_own_waiting_entries():
    own_waiting = item("Halter", minutes=20, holds_slot=True)
    foreign = item("Fremd", minutes=60)
    plan = build_plan([running(1, 30)], [own_waiting, foreign], NOW, backlog=[waiting()], free_slots=1,
                      insert_next=True)
    assert [b[1] for b in bars(plan, 1)] == ["", "Halter", "Clip", "Fremd"]


def test_released_prints_free_slots_for_the_next():
    """Limit 1, three prints: each is handed over when the one before it ends."""
    plan = build_plan([printer(1), printer(2)], [], NOW,
                      backlog=[waiting("A"), waiting("B"), waiting("C")], free_slots=1)
    starts = sorted((b[2], b[1]) for pid in (1, 2) for b in bars(plan, pid))
    assert starts == [(NOW, "A"), (at(10, 30), "B"), (at(11), "C")]
    assert plan.finish == at(11, 30)


def test_backlog_fixed_printer_goes_on_that_printer():
    plan = build_plan([printer(1), printer(2)], [], NOW, backlog=[waiting(printer_id=2)], free_slots=1)
    assert bars(plan, 2) == [("backlog", "Clip", NOW, at(10, 30))] and bars(plan, 1) == []


def test_backlog_fixed_printer_out_of_service_waits_in_its_lane_and_keeps_its_slot():
    offline = printer(2, st=status(2, connected=False))
    plan = build_plan([printer(1), offline], [], NOW, backlog=[waiting("A", printer_id=2), waiting("B")],
                      free_slots=1)
    assert plan.lanes[1].waiting == ("A",)
    assert plan.without == ("Printer 2",)
    assert bars(plan, 1) == [] and plan.no_slot == 1  # A holds the only place, and never ends


@pytest.mark.parametrize("backlog", [
    [waiting("A", model="X1C")],  # no printer of the model
    [waiting("A", nozzle=0.6)],  # no printer with that nozzle
])
def test_backlog_without_ready_printer_counts_unplaced_and_keeps_its_slot(backlog):
    one = printer(1, st=status(1, nozzles=(0.4,)))
    plan = build_plan([one], [], NOW, backlog=backlog + [waiting("B")], free_slots=1)
    assert plan.unplaced == 1 and plan.no_slot == 1 and bars(plan, 1) == []


def test_backlog_never_gets_a_slot_counts_no_slot():
    """The open prints of Reprintbuddy never end in the plan: one waits for a manual start."""
    manual = item("Halter", manual=True, holds_slot=True)
    plan = build_plan([printer(1)], [manual], NOW, backlog=[waiting("A"), waiting("B")], free_slots=0)
    assert plan.no_slot == 2 and plan.manual == 1 and plan.finish is None


def test_finish_includes_backlog_and_unknown_duration_is_minimum():
    plan = build_plan([printer(1)], [item("Fremd", minutes=60)], NOW, backlog=[waiting(minutes=None)],
                      free_slots=1)
    assert plan.finish == at(12, 5) and plan.finish_is_minimum is True


def test_shortest_first_orders_released_backlog_like_bambuddy():
    long_foreign = item("Fremd", minutes=120)
    plan = build_plan([running(1, 30)], [long_foreign], NOW, backlog=[waiting(minutes=10)], free_slots=1,
                      shortest_first=True)
    assert [b[1] for b in bars(plan, 1)] == ["", "Clip", "Fremd"]


def test_release_at_the_same_moment_a_printer_frees_goes_first():
    """The own print ends 10:30 on printer 1; printer 2 frees at 10:30 too (10:25 + gap): the hand-over
    happens in the same round as the query that sees the end, so the print is there when printer 2 looks."""
    own = item("Halter", state="printing", printer_id=1, holds_slot=True)
    foreign = item("Fremd", minutes=60)
    plan = build_plan([running(1, 30), running(2, 25)], [own, foreign], NOW, backlog=[waiting()],
                      free_slots=0, insert_next=True)
    assert bars(plan, 2)[1] == ("backlog", "Clip", at(10, 30), at(11))


def test_gap_between_backlog_prints_on_one_printer():
    plan = build_plan([printer(1)], [], NOW, backlog=[waiting("A"), waiting("B")], free_slots=2)
    assert bars(plan, 1) == [("backlog", "A", NOW, at(10, 30)),
                             ("backlog", "B", at(10, 30) + GAP, at(11, 5))]


def test_parked_prints_wait_in_their_lane_without_a_place():
    """Prints the hand-over skips because their printer is in maintenance (p646): text in that lane."""
    maintenance = printer(2, active=False)
    plan = build_plan([printer(1), maintenance], [], NOW, backlog=[waiting("B")], free_slots=1,
                      parked=[(2, "A"), (9, "unknown printer")])
    assert plan.lanes[1].waiting == ("A",) and plan.without == ("Printer 2",)
    assert bars(plan, 1) == [("backlog", "B", NOW, at(10, 30))]


def test_bars_carry_the_bambuddy_id_of_their_entry():
    """The queue page takes the times of its prints from the plan by Bambuddy's id (p647)."""
    printing = item("Halter", state="printing", printer_id=1)
    fixed = item("Fest", printer_id=2)
    planned = item("Frei")
    plan = build_plan([running(1, 30), printer(2), printer(3)], [printing, fixed, planned], NOW,
                      backlog=[waiting()], free_slots=1)
    ids = {b.label: b.item_id for lane in plan.lanes for b in lane.bars}
    assert ids == {"Halter": printing.item_id, "Fest": fixed.item_id, "Frei": planned.item_id, "Clip": None}
    foreign_running = build_plan([running(1, 30)], [], NOW).lanes[0].bars[0]
    assert foreign_running.item_id is None  # a print without a queue entry
