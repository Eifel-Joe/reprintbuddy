"""The print plan laid out for the overview (Spec 10.2, 0.4.2): time axis, position of the bars, names, texts.

Times are naive UTC; the tests pin the display zone to Europe/Berlin (`conftest.fixed_display_zone`), so
09:00 UTC is 10:00 on the page in November.
"""

from datetime import datetime, timedelta

from factories import make_article, make_dispatch, make_entry
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.types import Printer, PrinterStatus, QueueItem
from reprintbuddy.domain.printerboard import PrinterBoard
from reprintbuddy.web import printplan_view as pv

NOW = datetime(2026, 11, 12, 9, 0)  # 10:00 in Berlin


def item(item_id: int, *, status="pending", printer_id=None, model="A1", seconds=3000, created_by="admin",
         file_name="Spare part.gcode.3mf", position=None, manual_start=False, **fields) -> QueueItem:
    return QueueItem(
        id=item_id, status=status, batch_id=None, position=position or item_id, printer_id=printer_id,
        target_model=None if printer_id else model, label=f"{file_name} / plate 1", created_by=created_by,
        library_file_id=1, plate_id=1, printer_name=None, manual_start=manual_start, waiting_reason=None,
        started_at=None, completed_at=None, error_message=None, print_time_seconds=seconds,
        file_name=file_name, **fields,
    )


def board(*statuses: PrinterStatus, printers=None) -> PrinterBoard:
    b = PrinterBoard(None, now=lambda: NOW)
    printers = printers or [Printer(i, f"Printer {i}", "A1") for i in (1, 2)]
    b.replace(printers, {s.printer_id: s for s in statuses})
    return b


def idle(pid: int) -> PrinterStatus:
    return PrinterStatus(pid, True, "IDLE", None, None, None, False, False)


def test_axis_spans_now_to_finish_at_least_two_hours():
    axis = pv.axis(NOW, NOW + timedelta(minutes=50))
    assert axis.end == NOW + timedelta(hours=2)
    later = pv.axis(NOW, NOW + timedelta(hours=3, minutes=20))
    assert later.end == NOW + timedelta(hours=3, minutes=20)
    assert [label for _pos, label in later.ticks] == ["11:00", "12:00", "13:00"]
    assert later.ticks[0][0] == 100 * 60 / 200


def test_axis_caps_at_24_hours():
    axis = pv.axis(NOW, NOW + timedelta(hours=30))
    assert axis.end == NOW + timedelta(hours=24)


def test_tick_step_grows_with_the_span():
    assert {label for _p, label in pv.axis(NOW, NOW + timedelta(hours=12)).ticks} == {
        "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"}
    assert [label for _p, label in pv.axis(NOW, NOW + timedelta(hours=20)).ticks] == [
        "12:00", "16:00", "20:00", "00:00", "04:00"]


def test_ticks_on_full_local_hours_across_dst():
    """Sunday 25 October 2026, 03:00 summer time becomes 02:00 winter time in Berlin."""
    night = datetime(2026, 10, 24, 22, 30)  # 00:30 summer time
    labels = [label for _p, label in pv.axis(night, night + timedelta(hours=4)).ticks]
    assert labels == ["01:00", "02:00", "02:00", "03:00"]


def test_bars_are_placed_and_cut_at_the_axis(db):
    long = item(5, seconds=30 * 3600)
    with db() as s:
        view = pv.plan_view(s, board(idle(1), idle(2)), [long], NOW, "en", known=True)
    (bar,) = view.lanes[0].bars
    assert (bar.left, bar.width) == (0.0, 100.0)
    assert bar.cut == "→ until Fri 16:00"
    assert view.finish == "Fri 16:00"


def test_own_entry_named_after_its_article_foreign_after_its_file(db):
    with db.begin() as s:
        st.set_setting(s, "bambuddy_username", "reprintbuddy", commit=False)
        article = make_article(s, name="Bracket")
        make_dispatch(s, article.id, status="queued", bambuddy_item_id=7)
    queue = [item(7, created_by="reprintbuddy", file_name="Bracket.gcode.3mf"),
             item(8, file_name="Spare part.gcode.3mf"), item(9, file_name="Lid.3mf", created_by="reprintbuddy")]
    with db() as s:
        view = pv.plan_view(s, board(idle(1), idle(2)), queue, NOW, "en", known=True)
    bars = [b for lane in view.lanes for b in lane.bars]
    assert sorted((b.label, b.own) for b in bars) == [("Bracket", True), ("Lid", True), ("Spare part", False)]
    assert bars[0].kind == "planned" and bars[0].kind_text == "probably assigned"


def test_running_bar_texts(db):
    running = PrinterStatus(1, True, "RUNNING", 63.0, 70, "Clip", False, False)
    with db() as s:
        view = pv.plan_view(s, board(running, idle(2)), [], NOW, "en", known=True)
    (bar,) = view.lanes[0].bars
    assert bar.detail == "63 % · 1 h 10 left"
    assert (bar.start_text, bar.end_text, bar.kind_text) == ("10:00", "11:10", "printing")


def test_head_line_counts(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket")
        make_entry(s, a.id, 2)
        make_entry(s, a.id, 1, held=True)
    queue = [item(1, seconds=None), item(2, manual_start=True)]
    with db() as s:
        view = pv.plan_view(s, board(idle(1), idle(2)), queue, NOW, "en", known=True)
    assert view.waiting_here == 3
    assert view.manual == 1
    assert view.minimum is True
    assert view.finish == "11:00"


def test_stale_after_five_minutes(db):
    b = board(idle(1))
    with db() as s:
        assert pv.plan_view(s, b, [], NOW + timedelta(minutes=5), "en", known=True).stale == ""
        assert pv.plan_view(s, b, [], NOW + timedelta(minutes=6), "en", known=True).stale == "10:00"


def test_unknown_board_gives_unknown_plan(db):
    with db() as s:
        assert pv.plan_view(s, PrinterBoard(None), [], NOW, "en", known=True).known is False
        assert pv.plan_view(s, board(idle(1)), [], NOW, "en", known=False).known is False


def test_no_prints_planned(db):
    with db() as s:
        view = pv.plan_view(s, board(idle(1), idle(2)), [], NOW, "en", known=True)
    assert view.known and view.finish == "" and all(not lane.bars for lane in view.lanes)


def test_german_texts(db):
    running = PrinterStatus(1, True, "PAUSE", 40.0, 30, "Clip", False, False)
    with db() as s:
        view = pv.plan_view(s, board(running, idle(2)), [item(3, seconds=None)], NOW, "de", known=True)
    paused, unknown = view.lanes[0].bars[0], view.lanes[1].bars[0]
    assert paused.detail == "40 % · pausiert"
    assert unknown.detail == "Dauer unbekannt"
    assert view.lanes[0].bars[0].kind_text == "druckt"
    long = item(4, seconds=30 * 3600)
    with db() as s:
        view = pv.plan_view(s, board(idle(1), idle(2)), [long], NOW, "de", known=True)
    assert view.lanes[0].bars[0].cut == "→ bis Fr 16:00"


def test_no_stale_hint_while_the_live_channel_is_connected(db):
    """Connected, Bambuddy reports every change of a printer; quiet printers send nothing, so the age of
    the last message says nothing about the state (review v0.4.2, 10)."""
    b = board(idle(1))
    with db() as s:
        assert pv.plan_view(s, b, [], NOW + timedelta(hours=1), "en", known=True, live=True).stale == ""
        assert pv.plan_view(s, b, [], NOW + timedelta(hours=1), "en", known=True, live=False).stale == "10:00"


def test_stale_hint_names_the_day_when_older(db):
    b = board(idle(1))
    with db() as s:
        assert pv.plan_view(s, b, [], NOW + timedelta(days=1), "en", known=True).stale == "Thu 10:00"
