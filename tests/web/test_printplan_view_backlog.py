"""Reprintbuddy's own queue fed into the print plan of the overview (Spec 10.2, p646).

09:00 UTC is 10:00 on the page (Europe/Berlin in November, `conftest.fixed_display_zone`).
"""

from datetime import datetime

from factories import make_article, make_dispatch, make_entry
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.types import Printer, PrinterStatus, QueueItem
from reprintbuddy.domain.printerboard import FileFacts, PrinterBoard
from reprintbuddy.web import printplan_view as pv

NOW = datetime(2026, 11, 19, 9, 0)  # 10:00 in Berlin


def idle(pid: int) -> PrinterStatus:
    return PrinterStatus(pid, True, "IDLE", None, None, None, False, False)


def board(*statuses: PrinterStatus, printers=None, facts=None) -> PrinterBoard:
    b = PrinterBoard(None, now=lambda: NOW)
    printers = printers or [Printer(i, f"Printer {i}", "A1") for i in (1, 2)]
    b.replace(printers, {s.printer_id: s for s in statuses})
    b.file_facts = dict(facts or {})
    return b


def facts(file_id=1, fingerprint="v1", *, seconds=1800, plate=1, nozzle=None):
    return {(file_id, fingerprint): FileFacts(nozzle=nozzle, plate_seconds={plate: seconds})}


def article(s, name="Clip", **fields):
    fields.setdefault("target_model", "A1")
    fields.setdefault("file_fingerprint", "v1")
    return make_article(s, name=name, library_file_id=fields.pop("library_file_id", 1),
                        plate_id=fields.pop("plate_id", 1), **fields)


def queue_item(item_id: int, *, status="pending", printer_id=None, seconds=3600) -> QueueItem:
    return QueueItem(
        id=item_id, status=status, batch_id=None, position=item_id, printer_id=printer_id,
        target_model=None if printer_id else "A1", label="x / plate 1", created_by="someone",
        library_file_id=9, plate_id=1, printer_name=None, manual_start=False, waiting_reason=None,
        started_at=None, completed_at=None, error_message=None, print_time_seconds=seconds,
        file_name="Spare part.gcode.3mf",
    )


def view(db, b, queue=(), lang="en", **kw):
    with db() as s:
        return pv.plan_view(s, b, list(queue), NOW, lang, known=True, **kw)


def bars(v):
    return [(lane.name, bar.kind, bar.label, bar.start_text, bar.end_text) for lane in v.lanes for bar in lane.bars]


def test_queue_entries_are_planned_with_the_article_name_and_plate_time(db):
    with db.begin() as s:
        make_entry(s, article(s).id)
    v = view(db, board(idle(1), idle(2), facts=facts()))
    assert bars(v) == [("Printer 1", "backlog", "Clip", "10:00", "10:30")]
    assert v.lanes[0].bars[0].kind_text == "waiting in Reprintbuddy"
    assert v.finish == "10:30" and (v.held, v.no_slot, v.stopped) == (0, 0, 0)


def test_entry_over_several_prints_gives_one_bar_per_print(db):
    with db.begin() as s:
        make_entry(s, article(s).id, 3)
    v = view(db, board(idle(1), idle(2), facts=facts()))
    assert sorted(b[1:] for b in bars(v)) == [("backlog", "Clip", "10:00", "10:30")] * 2 + [
        ("backlog", "Clip", "10:35", "11:05")]


def test_held_entries_and_paused_articles_are_counted_not_planned(db):
    with db.begin() as s:
        make_entry(s, article(s, "Held").id, 2, held=True)
        make_entry(s, article(s, "Paused", plate_id=2, paused=True).id)
        make_entry(s, article(s, "No target", plate_id=3, target_model=None).id)
    v = view(db, board(idle(1), idle(2), facts=facts()))
    assert bars(v) == [] and v.held == 4 and v.finish == ""


def test_stopped_queue_plans_nothing_and_counts_all_prints(db):
    with db.begin() as s:
        st.set_setting(s, "queue_running", False, commit=False)
        make_entry(s, article(s).id, 2)
        make_entry(s, article(s, "Held", plate_id=2).id, 1, held=True)
    v = view(db, board(idle(1), idle(2), facts=facts()))
    assert bars(v) == [] and (v.stopped, v.held, v.no_slot) == (3, 0, 0)


def test_automation_off_still_plans_the_queue(db):
    """Automation off stops only the rule; the hand-over goes on (Spec 10.2, `workqueue.py:373`)."""
    with db.begin() as s:
        st.set_setting(s, "automation", False, commit=False)
        make_entry(s, article(s).id)
    assert [b[1] for b in bars(view(db, board(idle(1), facts=facts())))] == ["backlog"]


def test_article_waiting_for_its_printer_shows_in_that_lane(db):
    """Fixed printer in maintenance: the hand-over skips it (`engine.py:441`); text in that lane, no place."""
    printers = [Printer(1, "Printer 1", "A1"), Printer(2, "Printer 2", "A1", active=False)]
    with db.begin() as s:
        a = article(s, printer_id=2, target_model=None)
        make_entry(s, a.id)
        make_entry(s, article(s, "Other", plate_id=2).id)
        st.set_setting(s, "queue_cap", 1, commit=False)
    both = {(1, "v1"): FileFacts(nozzle=None, plate_seconds={1: 1800, 2: 3600})}
    v = view(db, board(idle(1), idle(2), printers=printers, facts=both), waiting_for_printer=[a.id])
    assert v.lanes[1].waiting == ("Clip",) and v.without == ("Printer 2",)
    # the parked print takes no place under the limit of 1: "Other" gets it
    assert bars(v) == [("Printer 1", "backlog", "Other", "10:00", "11:00")] and v.no_slot == 0


def test_model_with_every_printer_in_maintenance_counts_without_ready_printer(db):
    printers = [Printer(1, "Printer 1", "A1", active=False)]
    with db.begin() as s:
        a = article(s)
        make_entry(s, a.id, 2)
    v = view(db, board(idle(1), printers=printers, facts=facts()), waiting_for_printer=[a.id])
    assert bars(v) == [] and v.unplaced == 2 and v.no_slot == 0


def test_free_slots_follow_the_limit_and_open_prints(db):
    """Limit 1, the open print of Reprintbuddy runs till 11:00: the first print is handed over then, the
    second when the first ends; limit 2: the first goes at once."""
    running = PrinterStatus(1, True, "RUNNING", 50.0, 60, "Halter", False, False)
    with db.begin() as s:
        st.set_setting(s, "queue_cap", 1, commit=False)
        make_dispatch(s, article(s, "Halter", plate_id=2).id, status="printing", bambuddy_item_id=7)
        make_entry(s, article(s).id, 2)
    printing = [queue_item(7, status="printing", printer_id=1)]
    v = view(db, board(running, idle(2), facts=facts()), printing)
    assert sorted(b[1:] for b in bars(v) if b[1] == "backlog") == [
        ("backlog", "Clip", "11:00", "11:30"), ("backlog", "Clip", "11:30", "12:00")]
    with db.begin() as s:
        st.set_setting(s, "queue_cap", 2, commit=False)
    v = view(db, board(running, idle(2), facts=facts()), printing)
    assert sorted(b[1:] for b in bars(v) if b[1] == "backlog") == [
        ("backlog", "Clip", "10:00", "10:30"), ("backlog", "Clip", "10:35", "11:05")]


def test_open_print_not_in_bambuddys_list_never_frees_its_place(db):
    """A print being sent (no Bambuddy id yet) counts against the limit and ends nowhere in the plan."""
    with db.begin() as s:
        st.set_setting(s, "queue_cap", 1, commit=False)
        a = article(s)
        make_dispatch(s, a.id, status="sending")
        make_entry(s, a.id)
    v = view(db, board(idle(1), facts=facts()))
    assert bars(v) == [] and v.no_slot == 1


def test_insert_next_setting_reaches_the_plan(db):
    running = PrinterStatus(1, True, "RUNNING", 50.0, 30, "Halter", False, False)
    with db.begin() as s:
        st.set_setting(s, "queue_cap", 1, commit=False)
        st.set_setting(s, "insert_next", True, commit=False)
        make_dispatch(s, article(s, "Halter", plate_id=2).id, status="printing", bambuddy_item_id=7)
        make_entry(s, article(s).id)
    queue = [queue_item(7, status="printing", printer_id=1), queue_item(8)]
    one = [Printer(1, "Printer 1", "A1")]
    v = view(db, board(running, printers=one, facts=facts()), queue)
    assert [b[2] for b in bars(v)] == ["Halter", "Clip", "Spare part"]
    with db.begin() as s:
        st.set_setting(s, "insert_next", False, commit=False)
    v = view(db, board(running, printers=one, facts=facts()), queue)
    assert [b[2] for b in bars(v)] == ["Halter", "Spare part", "Clip"]


def test_unknown_plate_time_counts_one_hour_minimum(db):
    with db.begin() as s:
        make_entry(s, article(s).id)
    v = view(db, board(idle(1), idle(2)))  # nothing read yet for the file
    assert bars(v) == [("Printer 1", "backlog", "Clip", "10:00", "11:00")]
    assert v.minimum is True and v.lanes[0].bars[0].detail == "Duration unknown"


def test_nozzle_of_the_file_is_heeded(db):
    printers = [Printer(1, "Printer 1", "A1"), Printer(2, "Printer 2", "A1")]
    small = PrinterStatus(1, True, "IDLE", None, None, None, False, False, nozzles=(0.4,))
    big = PrinterStatus(2, True, "IDLE", None, None, None, False, False, nozzles=(0.6,))
    with db.begin() as s:
        make_entry(s, article(s).id)
    v = view(db, board(small, big, printers=printers, facts=facts(nozzle=0.6)))
    assert bars(v) == [("Printer 2", "backlog", "Clip", "10:00", "10:30")]


def test_empty_own_queue_gives_todays_plan(db):
    """Without entries in Reprintbuddy's queue the bars and numbers are those of 1.2.0."""
    queue = [queue_item(1), queue_item(2, printer_id=2), queue_item(3)]
    v = view(db, board(idle(1), idle(2)), queue)
    assert bars(v) == [("Printer 1", "planned", "Spare part", "10:00", "11:00"),
                       ("Printer 1", "planned", "Spare part", "11:05", "12:05"),
                       ("Printer 2", "fixed", "Spare part", "10:00", "11:00")]
    assert (v.held, v.no_slot, v.stopped, v.unplaced, v.manual) == (0, 0, 0, 0, 0)
