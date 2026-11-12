"""The print plan's assignment, for display only (Spec 10.2, 0.4.2): a pure function, no clock, no database."""

from datetime import datetime, timedelta

from reprintbuddy.bambuddy.types import PrinterStatus
from reprintbuddy.domain.printplan import GAP, PlanItem, PlanPrinter, build_plan, same_model

NOW = datetime(2026, 11, 12, 10, 0)


def at(hour: int, minute: int = 0) -> datetime:
    return NOW.replace(hour=hour, minute=minute)


def status(pid: int, state="IDLE", **kw) -> PrinterStatus:
    values = dict(connected=True, state=state, progress=None, remaining_min=None, subtask_name=None,
                  awaiting_plate_clear=False, stopped_error=False)
    values.update(kw)
    return PrinterStatus(pid, **values)


def printer(pid: int, *, model="A1", location=None, active=True, st: PrinterStatus | None | str = "idle") -> PlanPrinter:
    return PlanPrinter(pid, f"Printer {pid}", model, location, active, status(pid) if st == "idle" else st)


_ids = iter(range(1, 10_000))


def item(label="Clip", *, minutes: int | None = 50, own=True, printer_id=None, model="A1", location=None,
         state="pending", manual=False, later=False, jumped=False) -> PlanItem:
    return PlanItem(
        item_id=next(_ids), label=label, own=own, status=state, printer_id=printer_id,
        target_model=None if printer_id else model, target_location=location,
        duration=None if minutes is None else timedelta(minutes=minutes),
        manual_start=manual, starts_later=later, been_jumped=jumped,
    )


def bars(plan, pid):
    lane = next(lane for lane in plan.lanes if lane.printer_id == pid)
    return [(b.kind, b.label, b.start, b.end) for b in lane.bars]


def test_gap_is_five_minutes():
    assert GAP == timedelta(minutes=5)


def test_empty_queue_idle_printers():
    plan = build_plan([printer(1), printer(2)], [], NOW)
    assert [lane.printer_id for lane in plan.lanes] == [1, 2]
    assert all(lane.bars == () and lane.condition == "ready" for lane in plan.lanes)
    assert plan.finish is None and plan.unplaced == 0 and plan.manual == 0


def test_running_print_ends_after_remaining_minutes():
    running = printer(1, st=status(1, "RUNNING", progress=63.0, remaining_min=70))
    plan = build_plan([running], [], NOW)
    (bar,) = plan.lanes[0].bars
    assert (bar.kind, bar.start, bar.end, bar.progress) == ("running", NOW, at(11, 10), 63.0)
    assert plan.finish == at(11, 10)


def test_next_print_starts_after_a_five_minute_gap():
    running = printer(1, st=status(1, "RUNNING", progress=63.0, remaining_min=70))
    plan = build_plan([running], [item("Clip", minutes=50)], NOW)
    assert bars(plan, 1)[1] == ("planned", "Clip", at(11, 15), at(12, 5))


def test_planned_goes_to_the_printer_free_first():
    early = printer(1, st=status(1, "RUNNING", remaining_min=70))   # free 11:15
    late = printer(2, st=status(2, "RUNNING", remaining_min=135))   # free 12:20
    plan = build_plan([early, late], [item("Clip")], NOW)
    assert bars(plan, 1)[1][:2] == ("planned", "Clip")
    assert len(bars(plan, 2)) == 1


def test_two_printers_free_at_the_same_time_lower_id_wins():
    plan = build_plan([printer(2), printer(1)], [item("Clip")], NOW)
    assert bars(plan, 1) == [("planned", "Clip", NOW, at(10, 50))]
    assert bars(plan, 2) == []


def test_fixed_entry_stays_on_its_printer_in_queue_order():
    plan = build_plan([printer(1), printer(2)], [item("Lid", printer_id=2, minutes=30), item("Clip")], NOW)
    assert bars(plan, 2) == [("fixed", "Lid", NOW, at(10, 30))]
    assert bars(plan, 1) == [("planned", "Clip", NOW, at(10, 50))]


def test_model_filter_and_location():
    plan = build_plan(
        [printer(1, model="A1"), printer(2, model="P1S", location="B"), printer(3, model="P1S", location="C")],
        [item("Guide", model="P1S", location="C"), item("Hook", model="P1S", location="B")], NOW,
    )
    assert bars(plan, 3)[0][1] == "Guide" and bars(plan, 2)[0][1] == "Hook" and bars(plan, 1) == []
    assert same_model("Bambu Lab A1 ", "a1")
    assert not same_model("A1", "A1 mini")
    assert not same_model(None, "A1")


def test_foreign_items_are_planned_and_marked():
    plan = build_plan([printer(1)], [item("Spare part", own=False)], NOW)
    assert plan.lanes[0].bars[0].own is False


def test_offline_printer_keeps_stale_values_out():
    """Bambuddy keeps the last values of a printer that went silent (`bambu_mqtt.py:1878`): it is offline,
    shows no print and gets nothing; a printer without any reported state counts the same."""
    silent = printer(1, st=status(1, connected=False, state=None))
    unknown = printer(2, st=None)
    plan = build_plan([silent, unknown, printer(3)], [item("Clip")], NOW)
    assert [lane.condition for lane in plan.lanes] == ["offline", "offline", "ready"]
    assert bars(plan, 1) == [] and bars(plan, 2) == []
    assert bars(plan, 3)[0][1] == "Clip"


def test_out_of_service_printer_lists_its_fixed_entries():
    lanes = [
        printer(1, active=False),
        printer(2, st=status(2, stopped_error=True)),
        printer(3, st=status(3, connected=False, state=None)),
        printer(4),
    ]
    plan = build_plan(lanes, [item("Lid", printer_id=3), item("Clip"), item("Cap", printer_id=4, minutes=20)], NOW)
    assert [lane.condition for lane in plan.lanes] == ["maintenance", "error", "offline", "ready"]
    assert plan.lanes[2].waiting == ("Lid",) and plan.lanes[2].bars == ()
    assert plan.without == ("Printer 3",)
    assert bars(plan, 4) == [("planned", "Clip", NOW, at(10, 50)), ("fixed", "Cap", at(10, 55), at(11, 15))]
    assert plan.finish == at(11, 15)


def test_paused_print_with_an_error_stays_paused():
    paused = printer(1, st=status(1, "PAUSE", progress=40.0, remaining_min=30, stopped_error=True))
    plan = build_plan([paused], [], NOW)
    assert plan.lanes[0].condition == "ready"
    assert plan.lanes[0].bars[0].paused is True


def test_plate_waiting_printer_is_free_now():
    plate = printer(1, st=status(1, "FINISH", awaiting_plate_clear=True))
    plan = build_plan([plate], [item("Clip")], NOW)
    assert plan.lanes[0].condition == "plate"
    assert bars(plan, 1) == [("planned", "Clip", NOW, at(10, 50))]


def test_paused_print_counts_its_rest_from_now():
    paused = printer(1, st=status(1, "PAUSE", progress=40.0, remaining_min=30))
    (bar,) = build_plan([paused], [], NOW).lanes[0].bars
    assert bar.paused is True and bar.end == at(10, 30)


def test_running_without_remaining_time():
    """Preparing or calibrating: no remaining time yet (`remaining_time` 0). The printing entry's print
    time and the progress give the rest; without them one hour, marked as unknown."""
    preparing = printer(1, st=status(1, "PREPARE", progress=25.0, remaining_min=0))
    printing = item("Clip", minutes=120, state="printing", printer_id=1)
    (bar,) = build_plan([preparing], [printing], NOW).lanes[0].bars
    assert (bar.label, bar.end, bar.duration_known) == ("Clip", at(11, 30), True)
    blind = printer(1, st=status(1, "RUNNING", progress=None, remaining_min=None, subtask_name="Bowl"))
    plan = build_plan([blind], [], NOW)
    (bar,) = plan.lanes[0].bars
    assert (bar.label, bar.own, bar.end, bar.duration_known) == ("Bowl", False, at(11), False)
    assert plan.finish_is_minimum is True


def test_running_print_is_named_after_its_entry():
    running = printer(1, st=status(1, "RUNNING", progress=10.0, remaining_min=60, subtask_name="plate_1"))
    plan = build_plan([running], [item("Bracket", state="printing", printer_id=1, own=True)], NOW)
    (bar,) = plan.lanes[0].bars
    assert (bar.label, bar.own) == ("Bracket", True)


def test_unknown_duration_counts_one_hour_and_marks_minimum():
    plan = build_plan([printer(1)], [item("Spare part", minutes=None, own=False)], NOW)
    (bar,) = plan.lanes[0].bars
    assert (bar.end, bar.duration_known) == (at(11), False)
    assert plan.finish_is_minimum is True


def test_manual_and_later_entries_are_only_counted():
    plan = build_plan([printer(1)], [item("A", manual=True), item("B", later=True), item("C")], NOW)
    assert plan.manual == 2
    assert [b[1] for b in bars(plan, 1)] == ["C"]


def test_item_without_ready_printer_is_counted_not_placed():
    plan = build_plan(
        [printer(1, model="A1"), printer(2, model="P1S", st=status(2, connected=False, state=None))],
        [item("X", model="H2D"), item("Y", model="P1S"), item("Z", printer_id=9), item("C")], NOW,
    )
    assert plan.unplaced == 3
    assert [b[1] for b in bars(plan, 1)] == ["C"]
    assert plan.finish == at(10, 50)
    assert plan.without == ()


def test_finish_is_the_latest_end():
    plan = build_plan([printer(1), printer(2)], [item("A", minutes=30), item("B", minutes=90), item("C", minutes=20)], NOW)
    assert plan.finish == at(11, 30)
    assert bars(plan, 1)[1][1:] == ("C", at(10, 35), at(10, 55))


def test_shortest_first_like_bambuddy():
    """Bambuddy's "shortest job first" (`print_scheduler.py:1700-1739`): entries already jumped first, then the
    shorter print, unknown print time last, the queue order breaks ties."""
    queue = [item("Long", minutes=120), item("Unknown", minutes=None), item("Short", minutes=20),
             item("Jumped", minutes=200, jumped=True), item("Short too", minutes=20)]
    plain = build_plan([printer(1)], queue, NOW)
    assert [b[1] for b in bars(plain, 1)] == ["Long", "Unknown", "Short", "Jumped", "Short too"]
    sjf = build_plan([printer(1)], queue, NOW, shortest_first=True)
    assert [b[1] for b in bars(sjf, 1)] == ["Jumped", "Short", "Short too", "Long", "Unknown"]
