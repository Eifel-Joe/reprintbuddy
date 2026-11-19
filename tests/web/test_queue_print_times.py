"""Start, end and print time of the prints in progress on the queue page, from the print plan (Spec 10.3a, p647).

09:00 UTC is 10:00 on the page (Europe/Berlin in November, `conftest.fixed_display_zone`).
"""

from datetime import datetime, timedelta

from factories import make_article, make_dispatch
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


def queue_item(item_id: int, *, status="pending", printer_id=None, model="A1", seconds: int | None = 3600,
               started_at=None) -> QueueItem:
    return QueueItem(
        id=item_id, status=status, batch_id=None, position=item_id, printer_id=printer_id,
        target_model=None if printer_id else model, label="x / plate 1", created_by="reprintbuddy",
        library_file_id=1, plate_id=1, printer_name=None, manual_start=False, waiting_reason=None,
        started_at=started_at, completed_at=None, error_message=None, print_time_seconds=seconds,
        file_name="Clip.gcode.3mf",
    )


def dispatch(db, *, status="queued", item_id=None) -> int:
    with db.begin() as s:
        a = make_article(s, name=f"Clip {item_id}", target_model="A1", file_fingerprint="v1")
        return make_dispatch(s, a.id, status=status, bambuddy_item_id=item_id,
                             library_file_id=a.library_file_id, plate_id=a.plate_id).id


def times(db, b, queue, lang="en", **kw):
    kw.setdefault("known", True)
    with db() as s:
        return pv.in_progress_times(s, b, queue, NOW, lang, **kw)


def test_printing_starts_when_bambuddy_started_it_and_ends_with_the_running_bar(db):
    running = PrinterStatus(1, True, "RUNNING", 50.0, 60, "Clip", False, False)
    d = dispatch(db, status="printing", item_id=7)
    started = NOW - timedelta(minutes=48)
    t = times(db, board(running, idle(2)), [queue_item(7, status="printing", printer_id=1, started_at=started)])
    assert t[d] == pv.PrintTimes(start="09:12", end="about 11:00", duration="1 h 00")


def test_printing_without_a_start_from_bambuddy_says_running(db):
    running = PrinterStatus(1, True, "RUNNING", 50.0, 60, "Clip", False, False)
    d = dispatch(db, status="printing", item_id=7)
    t = times(db, board(running, idle(2)), [queue_item(7, status="printing", printer_id=1)], lang="de")
    assert t[d] == pv.PrintTimes(start="läuft", end="ca. 11:00", duration="1 h 00")


def test_queued_gets_start_and_end_of_its_bar(db):
    running = PrinterStatus(1, True, "RUNNING", 50.0, 30, "Other", False, False)
    d = dispatch(db, item_id=8)
    t = times(db, board(running, printers=[Printer(1, "Printer 1", "A1")]), [queue_item(8, seconds=50 * 60)])
    assert t[d] == pv.PrintTimes(start="about 10:35", end="about 11:25", duration="50 min")


def test_unknown_duration_ends_at_the_earliest(db):
    d = dispatch(db, item_id=8)
    t = times(db, board(idle(1), idle(2)), [queue_item(8, seconds=None)])
    assert t[d] == pv.PrintTimes(start="about 10:00", end="about 11:00 at the earliest", duration="Duration unknown")
    t = times(db, board(idle(1), idle(2)), [queue_item(8, seconds=None)], lang="de")
    assert t[d] == pv.PrintTimes(start="ca. 10:00", end="mindestens ca. 11:00", duration="Dauer unbekannt")


def test_another_day_names_the_weekday(db):
    running = PrinterStatus(1, True, "RUNNING", 10.0, 20 * 60, "Other", False, False)
    d = dispatch(db, item_id=8)
    t = times(db, board(running, printers=[Printer(1, "Printer 1", "A1")]), [queue_item(8)])
    assert t[d].start == "about Fri 06:05" and t[d].end == "about Fri 07:05"


def test_not_in_the_lanes_has_no_times_but_its_print_time(db):
    d = dispatch(db, item_id=8)
    t = times(db, board(idle(1), idle(2)), [queue_item(8, model="X1C", seconds=1800)])
    assert t[d] == pv.PrintTimes(start="", end="", duration="30 min")


def test_being_handed_over_takes_the_print_time_of_the_file(db):
    d = dispatch(db, status="sending")
    file_facts = {(1, "v1"): FileFacts(nozzle=None, plate_seconds={1: 4200})}
    t = times(db, board(idle(1), facts=file_facts), [])
    assert t[d] == pv.PrintTimes(start="", end="", duration="1 h 10")
    assert times(db, board(idle(1)), [])[d].duration == "Duration unknown"


def test_unknown_printer_state_has_no_times(db):
    d = dispatch(db, item_id=8)
    t = times(db, board(idle(1), idle(2)), [queue_item(8)], known=False)
    assert t[d] == pv.PrintTimes(start="", end="", duration="1 h 00")
    t = times(db, PrinterBoard(None), [queue_item(8)])
    assert t[d] == pv.PrintTimes(start="", end="", duration="1 h 00")


def test_reprintbuddys_own_queue_moves_nothing_of_the_handed_over_prints(db):
    """A print still in Reprintbuddy goes behind the handed-over ones; their times stay (p646, p647)."""
    from factories import make_entry

    d = dispatch(db, item_id=8)
    with db.begin() as s:
        make_entry(s, make_article(s, name="Later", target_model="A1", plate_id=5).id, 3)
    t = times(db, board(idle(1), printers=[Printer(1, "Printer 1", "A1")]), [queue_item(8)])
    assert t[d] == pv.PrintTimes(start="about 10:00", end="about 11:00", duration="1 h 00")
