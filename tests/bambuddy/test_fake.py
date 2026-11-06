import inspect

import pytest

from reprintbuddy.bambuddy.client import (
    BambuddyClient,
    BambuddyRejected,
    BambuddyUnavailable,
)
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import FilamentRequirement, Printer


def test_fake_is_client():
    assert isinstance(FakeBambuddy(), BambuddyClient)


def test_protocol_matches_master_plan():
    names = {n for n in dir(BambuddyClient) if not n.startswith("_")}
    assert names == {
        "ping", "list_folders", "list_files", "get_file", "list_plates",
        "plate_thumbnail", "filament_requirements", "ensure_batch", "enqueue",
        "get_queue_item", "list_queue", "find_items_by_batch", "list_spools",
        "list_printers",
    }
    for name in names:
        assert inspect.iscoroutinefunction(getattr(BambuddyClient, name)), name

    def params(name):
        return [p for p in inspect.signature(getattr(BambuddyClient, name)).parameters if p != "self"]

    assert params("list_files") == ["folder_id"]
    assert params("plate_thumbnail") == ["file_id", "plate_index"]
    assert params("filament_requirements") == ["file_id", "plate_id"]


async def test_folders_and_files():
    fake = FakeBambuddy()
    folder = fake.add_folder("Templates")
    file = fake.add_file("Template A.gcode.3mf", folder.id)
    assert await fake.list_files(folder.id) == [file]
    assert await fake.list_files(None) == []
    assert (await fake.get_file(file.id)).printer_model == "A1"
    assert await fake.list_folders() == [folder]


async def test_plates_numbered_from_one():
    fake = FakeBambuddy()
    f = fake.add_file("Template B.gcode.3mf", plates=2)
    assert [p.index for p in await fake.list_plates(f.id)] == [1, 2]


async def test_thumbnail_png():
    fake = FakeBambuddy()
    f = fake.add_file("Template B.gcode.3mf", plates=2)
    one = await fake.plate_thumbnail(f.id, 1)
    two = await fake.plate_thumbnail(f.id, 2)
    assert one.startswith(b"\x89PNG")
    assert one != two
    with pytest.raises(BambuddyRejected):
        await fake.plate_thumbnail(999, 1)


async def test_requirements():
    fake = FakeBambuddy()
    f = fake.add_file("Template A.gcode.3mf", plates=2)
    reqs = [FilamentRequirement(1, 42.5, "PLA", "#000000")]
    fake.set_requirements(f.id, 1, reqs)
    assert await fake.filament_requirements(f.id, 1) == reqs
    assert await fake.filament_requirements(f.id, 2) == []


async def test_printers():
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    fake.add_printer("Printer 2", "P1S")
    assert await fake.list_printers() == [Printer(1, "Printer 1", "A1"), Printer(2, "Printer 2", "P1S")]


async def test_unavailable():
    fake = FakeBambuddy()
    fake.unavailable = True
    assert await fake.ping() is False
    with pytest.raises(BambuddyUnavailable):
        await fake.list_folders()


async def test_removed_file():
    fake = FakeBambuddy()
    f = fake.add_file("Template A.gcode.3mf")
    fake.remove_file(f.id)
    assert await fake.get_file(f.id) is None
    with pytest.raises(BambuddyRejected):
        await fake.list_plates(f.id)


async def test_calls_recorded():
    fake = FakeBambuddy()
    await fake.ping()
    await fake.list_folders()
    assert fake.calls == ["ping", "list_folders"]
