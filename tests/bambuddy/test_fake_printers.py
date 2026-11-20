"""The Bambuddy stand-in for the print plan (0.4.2): printer states, print times, pinned entries."""

import pytest

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import EnqueueRequest, PrinterStatus


def _req(file_id: int) -> EnqueueRequest:
    return EnqueueRequest(
        library_file_id=file_id, plate_id=1, target_model="A1", printer_id=None, insert_at_top=False, insert_position=None,
    )


async def test_status_defaults_to_idle_and_connected():
    fake = FakeBambuddy()
    printer = fake.add_printer("Printer 1", "A1", location="Shelf B")
    assert printer.location == "Shelf B"
    found = await fake.printer_statuses([printer.id])
    assert found == {printer.id: PrinterStatus(printer.id, True, "IDLE", None, None, None, False, False)}


async def test_set_printer_status_is_returned():
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    fake.set_printer_status(1, state="RUNNING", progress=63.0, remaining_min=70, subtask_name="Clip")
    status = (await fake.printer_statuses([1]))[1]
    assert (status.state, status.progress, status.remaining_min, status.subtask_name) == ("RUNNING", 63.0, 70, "Clip")


async def test_offline_printer_drops_its_values_like_the_parser():
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    fake.set_printer_status(1, connected=False, state="RUNNING", progress=40.0, remaining_min=30)
    status = (await fake.printer_statuses([1]))[1]
    assert status.connected is False and status.state is None and status.progress is None


async def test_unknown_ids_are_left_out():
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    assert list(await fake.printer_statuses([1, 9])) == [1]


async def test_enqueue_carries_print_time_of_the_file():
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    file = fake.add_file("Clip.gcode.3mf", print_time_s=5400)
    item = await fake.enqueue(_req(file.id))
    assert item.print_time_seconds == 5400
    assert item.file_name == "Clip.gcode.3mf"
    unknown = fake.add_file("Old.gcode.3mf", print_time_s=None)
    assert (await fake.enqueue(_req(unknown.id))).print_time_seconds is None


async def test_foreign_item_can_be_pinned_to_a_printer():
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    fake.add_printer("Printer 2", "A1")
    item = fake.add_foreign_item("Lid", printer_id=2, print_time_s=1800, target_location="Shelf B")
    assert (item.printer_id, item.printer_name, item.print_time_seconds) == (2, "Printer 2", 1800)
    assert item.file_name == "Lid" and item.target_location == "Shelf B"
    assert [i.id for i in await fake.list_queue()] == [item.id]


async def test_plan_settings():
    from reprintbuddy.bambuddy.types import PlanSettings

    fake = FakeBambuddy()
    assert await fake.plan_settings() == PlanSettings(shortest_first=False, prefer_lowest=None)
    fake.shortest = True
    fake.prefer_lowest = True
    assert await fake.plan_settings() == PlanSettings(shortest_first=True, prefer_lowest=True)


@pytest.mark.parametrize("method", ["printer_statuses", "plan_settings"])
async def test_fail_next(method):
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    fake.fail_next(method, BambuddyRejected("no", status=403))
    call = fake.printer_statuses([1]) if method == "printer_statuses" else fake.plan_settings()
    with pytest.raises(BambuddyRejected):
        await call
    fake.unavailable = True
    with pytest.raises(BambuddyUnavailable):
        await (fake.printer_statuses([1]) if method == "printer_statuses" else fake.plan_settings())


async def test_foreign_item_for_any_printer_of_a_model():
    fake = FakeBambuddy()
    item = fake.add_foreign_item("Spare part", target_model="A1")
    assert (item.printer_id, item.target_model) == (None, "A1")


async def test_filament_slots_grams_and_shortage():
    """The stand-in for p648: slots in the printer's state, grams of assigned spools, Bambuddy's shortage flag."""
    from reprintbuddy.bambuddy.types import AmsSlot

    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    fake.set_printer_slots(1, [AmsSlot(0, 0, "PLA", "#000000", True), AmsSlot(0, 1, "PETG", "#FF0000", True)],
                           backup=True, grams={(0, 0): 640.0})
    status = (await fake.printer_statuses([1]))[1]
    assert [s.material for s in status.slots] == ["PLA", "PETG"] and status.backup is True
    assert await fake.inventory_remain(1) == {(0, 0): 640.0}
    assert await fake.inventory_remain(9) == {}
    item = fake.add_foreign_item("Short", filament_short=True)
    assert (await fake.list_queue())[-1].filament_short is True and item.filament_short is True
    fake.fail_next("inventory_remain", BambuddyRejected("no", status=403))
    with pytest.raises(BambuddyRejected):
        await fake.inventory_remain(1)
