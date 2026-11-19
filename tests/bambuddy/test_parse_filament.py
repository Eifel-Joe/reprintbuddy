"""What the print plan reads of the filament in the printers (Spec 7.6, 10.2, p648): the slots of the printer's
state, the backup switch, the grams Bambuddy holds for assigned spools and Bambuddy's own shortage flag."""

import pytest

from fixtures import API, client
from reprintbuddy.bambuddy.client import BambuddyUnexpected
from reprintbuddy.bambuddy.parse import parse_inventory_remain, parse_printer_status, parse_queue_item
from reprintbuddy.bambuddy.types import AmsSlot


def status(**fields) -> dict:
    base = {"id": 1, "connected": True, "state": "IDLE", "hms_errors": []}
    base.update(fields)
    return base


def tray(tid: int, material="PLA", color="000000FF", state=11, **kw) -> dict:
    return {"id": tid, "tray_type": material, "tray_color": color, "state": state, **kw}


def test_slots_of_the_ams_units_and_the_external_spool():
    """AMS slots keep Bambuddy's (unit, slot); an external spool is stored as (255, index), Bambuddy
    `utils/tray_ids.py:55-58, 190-194`."""
    s = parse_printer_status(status(
        ams=[{"id": 0, "tray": [tray(0), tray(1, "PETG", "FF0000FF")]},
             {"id": 128, "is_ams_ht": True, "tray": [tray(0, "TPU", "FFFFFFFF")]}],
        vt_tray=[tray(254, "PLA", "00FF0080"), tray(255, None, None, state=None)],
    ))
    assert s.slots == (
        AmsSlot(0, 0, "PLA", "#000000", True),
        AmsSlot(0, 1, "PETG", "#FF0000", True),
        AmsSlot(128, 0, "TPU", "#FFFFFF", True),
        AmsSlot(255, 0, "PLA", "#00FF00", True),
        AmsSlot(255, 1, None, None, False),
    )


@pytest.mark.parametrize("raw, loaded", [
    ({"state": 9}, False),  # empty
    ({"state": 10}, True),  # spool there, not loaded into the feeder: Bambuddy still matches by tray_type
    ({"state": None}, True),
    ({"state": 11, "tray_type": ""}, False),  # no material: Bambuddy skips it (`_build_loaded_filaments`)
])
def test_loaded_means_a_material_and_not_empty(raw, loaded):
    t = {**tray(0), **raw}
    s = parse_printer_status(status(ams=[{"id": 0, "tray": [t]}]))
    assert s.slots[0].loaded is loaded


def test_rack_external_ids_map_to_their_index():
    s = parse_printer_status(status(ams=[], vt_tray=[tray(247), tray(253)]))
    assert [(x.ams_id, x.tray_id) for x in s.slots] == [(255, 2), (255, 8)]


def test_no_ams_fields_means_not_reported():
    """A message without them (an older live message) keeps the last known slots on the board."""
    assert parse_printer_status(status()).slots is None
    assert parse_printer_status(status(ams=[], vt_tray=[])).slots == ()
    assert parse_printer_status(status(ams=None)).slots is None  # the live channel sends null without AMS


def test_odd_slot_entries_are_skipped_not_fatal():
    """Only shown: an entry Reprintbuddy cannot read is left out, the state still counts."""
    s = parse_printer_status(status(ams=[{"id": 0, "tray": ["x", {"tray_type": "PLA"}, tray(2)]}, "y"]))
    assert [x.tray_id for x in s.slots] == [2]


@pytest.mark.parametrize("raw, expected", [(True, True), (False, False), (None, None), ("x", None)])
def test_backup_switch(raw, expected):
    assert parse_printer_status(status(ams_filament_backup=raw)).backup is expected


def test_not_connected_drops_the_slots_too():
    """Spec 7.6: the last values of a printer that is not connected do not count."""
    s = parse_printer_status(status(connected=False, ams=[{"id": 0, "tray": [tray(0)]}], ams_filament_backup=True))
    assert s.slots is None and s.backup is None


def test_inventory_remain_by_slot():
    data = {
        "inventory_remain_g": {"0": 812.5},
        "slot_materials": [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0, "remaining_g": 812.5, "material_key": "k"},
            {"ams_id": 255, "tray_id": 0, "global_tray_id": 254, "remaining_g": 120},
            {"ams_id": 1, "tray_id": 3, "remaining_g": -4},  # nonsense: left out
            {"ams_id": 1, "remaining_g": 10},  # no slot: left out
        ],
    }
    assert parse_inventory_remain(data) == {(0, 0): 812.5, (255, 0): 120.0}


def test_inventory_remain_not_an_object_is_unexpected():
    with pytest.raises(BambuddyUnexpected):
        parse_inventory_remain([1, 2])
    assert parse_inventory_remain({"inventory_remain_g": {}, "slot_materials": []}) == {}


def test_queue_entry_filament_short():
    base = {"id": 3, "status": "pending", "manual_start": True}
    assert parse_queue_item({**base, "filament_short": True}).filament_short is True
    assert parse_queue_item(base).filament_short is False
    assert parse_queue_item({**base, "filament_short": "yes"}).filament_short is False


async def test_client_inventory_remain(respx_mock):
    respx_mock.get(f"{API}/printers/2/inventory-remain").respond(
        200, json={"inventory_remain_g": {}, "slot_materials": [{"ams_id": 0, "tray_id": 1, "remaining_g": 50}]})
    assert await client().inventory_remain(2) == {(0, 1): 50.0}
