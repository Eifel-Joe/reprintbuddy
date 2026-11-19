"""The reload hint in the print plan of the overview (Spec 10.2 "Filament in den Druckern", p648).

09:00 UTC is 10:00 on the page (Europe/Berlin in November, `conftest.fixed_display_zone`).
"""

from datetime import datetime, timedelta

from factories import make_article, make_dispatch, make_entry
from reprintbuddy.bambuddy.types import AmsSlot, FilamentRequirement, Printer, PrinterStatus, QueueItem
from reprintbuddy.domain.printerboard import FileFacts, PrinterBoard
from reprintbuddy.models import ArticleFilament
from reprintbuddy.web import printplan_view as pv

NOW = datetime(2026, 11, 19, 9, 0)  # 10:00 in Berlin
BLACK = AmsSlot(0, 1, "PLA", "#000000", True)


def status(pid: int, state="IDLE", *, slots=(BLACK,), backup=None, **kw) -> PrinterStatus:
    return PrinterStatus(pid, True, state, kw.get("progress"), kw.get("remaining"), None, False, False,
                         slots=slots, backup=backup)


def board(*statuses, grams=None, needs=None, facts=None, printers=None) -> PrinterBoard:
    b = PrinterBoard(None, now=lambda: NOW)
    b.replace(printers or [Printer(s.printer_id, f"Printer {s.printer_id}", "A1") for s in statuses],
              {s.printer_id: s for s in statuses})
    b.slot_grams = dict(grams or {})
    b.needs = dict(needs or {})
    b.file_facts = dict(facts or {})
    return b


def queue_item(item_id, *, status="pending", printer_id=None, seconds=3600, file_id=9, plate=1,
               filament_short=False) -> QueueItem:
    return QueueItem(
        id=item_id, status=status, batch_id=None, position=item_id, printer_id=printer_id,
        target_model=None if printer_id else "A1", label="x / plate 1", created_by="someone",
        library_file_id=file_id, plate_id=plate, printer_name=None, manual_start=False, waiting_reason=None,
        started_at=None, completed_at=None, error_message=None, print_time_seconds=seconds,
        file_name="Spare part.gcode.3mf", filament_short=filament_short,
    )


def article(s, name="Clip", grams=100.0, material="PLA", color="#000000", plate=1):
    a = make_article(s, name=name, target_model="A1", file_fingerprint="v1", plate_id=plate)
    s.add(ArticleFilament(article_id=a.id, slot=1, grams=grams, material=material, color_hex=color))
    s.flush()
    return a


def view(db, b, queue=(), lang="en"):
    with db() as s:
        return pv.plan_view(s, b, list(queue), NOW, lang, known=True)


def texts(lane):
    return [m.text for m in lane.markers]


def test_reload_marker_for_an_own_print_in_reprintbuddys_queue(db):
    """300 g left, two prints of 200 g, 10:00-11:00 and 11:05-12:05: dry half way through the second."""
    with db.begin() as s:
        make_entry(s, article(s, grams=200).id, 2)
    facts = {(1, "v1"): FileFacts(None, {1: 3600})}
    v = view(db, board(status(1), grams={1: {(0, 1): 300.0}}, facts=facts))
    (lane,) = v.lanes
    assert texts(lane) == ["Reload about 11:35 · PLA #000000 (AMS 1, slot 2)"]
    assert lane.markers[0].left is not None
    assert v.reload == "Reload needed: Printer 1 about 11:35"


def test_german_texts_and_another_day(db):
    with db.begin() as s:
        make_entry(s, article(s, grams=200).id, 2)
    facts = {(1, "v1"): FileFacts(None, {1: 20 * 3600})}
    v = view(db, board(status(1), grams={1: {(0, 1): 300.0}}, facts=facts), lang="de")
    assert texts(v.lanes[0]) == ["Nachladen ca. Fr 16:05 · PLA #000000 (AMS 1, Slot 2)"]
    assert v.reload == "Nachladen nötig: Printer 1 ca. Fr 16:05"


def test_running_own_print_counts_whole_and_can_be_due(db):
    """2 h print ending 10:12, so begun 08:12; 100 g needed, 30 g left: dry at 08:48, past → now."""
    with db.begin() as s:
        a = article(s, grams=100)
        make_dispatch(s, a.id, status="printing", bambuddy_item_id=7)
    running = status(1, "RUNNING", progress=90.0, remaining=12)
    v = view(db, board(running, grams={1: {(0, 1): 30.0}}),
             [queue_item(7, status="printing", printer_id=1, seconds=7200)])
    assert texts(v.lanes[0]) == ["Reload now · PLA #000000 (AMS 1, slot 2)"]
    assert v.reload == "Reload needed: Printer 1 now"


def test_foreign_print_needs_from_the_file_manager_or_unknown(db):
    need = (FilamentRequirement(1, 500.0, "PLA", "#000000"),)
    q = [queue_item(8, seconds=3600), queue_item(9, file_id=None, seconds=3600)]
    v = view(db, board(status(1), grams={1: {(0, 1): 300.0}}, needs={(9, 1): need}), q)
    lane = v.lanes[0]
    assert texts(lane) == ["Reload about 10:36 · PLA #000000 (AMS 1, slot 2)"]
    details = [b.detail for b in lane.bars]
    assert details == ["", "Filament need unknown"]


def test_unknown_need_before_makes_it_at_the_latest(db):
    need = (FilamentRequirement(1, 500.0, "PLA", "#000000"),)
    q = [queue_item(9, file_id=None, seconds=3600), queue_item(8, seconds=3600)]
    v = view(db, board(status(1), grams={1: {(0, 1): 300.0}}, needs={(9, 1): need}), q)
    assert texts(v.lanes[0]) == ["Reload at the latest about 11:41 · PLA #000000 (AMS 1, slot 2)"]
    assert v.reload == "Reload needed: Printer 1 at the latest about 11:41"


def test_not_loaded_and_unknown_stock(db):
    with db.begin() as s:
        make_entry(s, article(s, "Red", material="PETG", color="#FF0000").id)
        make_entry(s, article(s, "Black", plate=2).id)
    v = view(db, board(status(1)))  # no grams read for printer 1
    assert texts(v.lanes[0]) == ["Not loaded: PETG #FF0000", "Stock unknown: PLA #000000 (AMS 1, slot 2)"]
    assert all(m.left is None for m in v.lanes[0].markers)
    assert v.reload == ""


def test_external_spool_and_ams_ht_names():
    assert pv.slot_name("en", AmsSlot(255, 0, "PLA", "#000000", True)) == "external spool"
    assert pv.slot_name("en", AmsSlot(255, 1, "PLA", "#000000", True)) == "external spool 2"
    assert pv.slot_name("de", AmsSlot(128, 0, "TPU", "#FFFFFF", True)) == "AMS-HT 1"
    assert pv.slot_name("de", AmsSlot(1, 3, "PLA", "#000000", True)) == "AMS 2, Slot 4"


def test_more_than_three_markers_and_more_reloads_in_the_head_line(db):
    slots = tuple(AmsSlot(0, i, "PLA", c, True) for i, c in enumerate(["#000000", "#FFFFFF", "#00FF00", "#0000FF"]))
    with db.begin() as s:
        for i, c in enumerate(["#000000", "#FFFFFF", "#00FF00", "#0000FF"]):
            make_entry(s, article(s, f"A{i}", grams=100, color=c, plate=i + 1).id)
    grams = {(0, i): 10.0 for i in range(4)}
    facts = {(1, "v1"): FileFacts(None, {i + 1: 3600 for i in range(4)})}
    one = [Printer(1, "Printer 1", "A1")]
    v = view(db, board(status(1, slots=slots), grams={1: grams}, facts=facts, printers=one))
    assert len(v.lanes[0].markers) == 3 and v.lanes[0].more == 1
    assert v.reload == "Reload needed: Printer 1 about 10:06 and 3 more"


def test_bambuddys_own_shortage_is_counted(db):
    q = [queue_item(8, filament_short=True), queue_item(9, filament_short=True, status="completed")]
    v = view(db, board(status(1)), q)
    assert v.filament_short == 1


def test_lane_out_of_service_has_no_markers(db):
    with db.begin() as s:
        make_entry(s, article(s).id)
    off = PrinterStatus(1, False, None, None, None, None, False, False)
    v = view(db, board(off, status(2), printers=[Printer(1, "Printer 1", "A1"), Printer(2, "Printer 2", "A1")]))
    assert v.lanes[0].markers == ()


def test_without_filament_data_the_plan_is_unchanged(db):
    """No slots reported (an older Bambuddy): no markers, no head line, bars as before."""
    q = [queue_item(8)]
    v = view(db, board(status(1, slots=None)), q)
    assert v.lanes[0].markers == () and v.reload == "" and v.filament_short == 0
    assert [b.detail for b in v.lanes[0].bars] == [""]


def test_printer_reporting_empty_slot_lists_gets_no_notes(db):
    """Review p648, 2: Bambuddy sends `ams: []`, `vt_tray: []` for a printer without AMS (schema defaults):
    no notes, not "Not loaded" for every need (Spec 10.2: a printer that reports no slots)."""
    with db.begin() as s:
        make_entry(s, article(s).id)
    v = view(db, board(status(1, slots=())))
    assert v.lanes[0].markers == () and v.reload == ""


def test_need_without_material_and_colour_is_named():
    from reprintbuddy.domain.filamentplan import Marker as M

    assert pv._marker_text("en", M("not_loaded", None, None, None, None), NOW) == "Not loaded: unknown filament"


def test_marker_colour_must_be_a_plain_hex(db):
    """Review p648, 10: the colour goes into a style attribute; anything but #RRGGBB is dropped."""
    from reprintbuddy.domain.filamentplan import LaneFilament, Marker as M

    lane = LaneFilament((M("not_loaded", "PLA", "red;x:y", None, None),), 0, ())
    (view_,), _more = pv._markers(lane, pv.axis(NOW, None), NOW, "en")
    assert view_.color is None


def test_running_print_without_print_time_starts_from_its_progress(db):
    """Review p648, 9: 75 % done, 15 min left → it began 45 min ago (09:15 local); 100 g of 30 g: dry at 09:33."""
    with db.begin() as s:
        a = article(s, grams=100)
        make_dispatch(s, a.id, status="printing", bambuddy_item_id=7)
    running = status(1, "RUNNING", progress=75.0, remaining=15)
    v = view(db, board(running, grams={1: {(0, 1): 30.0}}),
             [queue_item(7, status="printing", printer_id=1, seconds=None)])
    assert texts(v.lanes[0]) == ["Reload now · PLA #000000 (AMS 1, slot 2)"]
