import asyncio
import time

from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import EnqueueRequest


async def _enqueue(fake: FakeBambuddy):
    return await fake.enqueue(EnqueueRequest(1, 1, "A1", None, False, None))


async def test_delay_lets_other_tasks_run(fake_bambuddy):
    fake_bambuddy.delay = 0.05
    stamps: list[float] = []

    async def marker():
        stamps.append(time.monotonic())

    async def reader():
        await fake_bambuddy.list_queue()
        stamps.append(time.monotonic())

    await asyncio.gather(reader(), marker())
    assert len(stamps) == 2
    assert stamps[0] < stamps[1]  # the marker ran before list_queue finished
    assert stamps[1] - stamps[0] >= 0.02


async def test_no_delay_by_default():
    assert FakeBambuddy().delay == 0.0


async def test_set_manual_start(fake_bambuddy):
    item = await _enqueue(fake_bambuddy)
    assert item.manual_start is False
    fake_bambuddy.set_manual_start(item.id, True)
    assert (await fake_bambuddy.get_queue_item(item.id)).manual_start is True


async def test_b4_needs_are_there(fake_bambuddy):
    for name in (
        "add_foreign_item", "set_status", "delete_item", "update_file",
        "fail_next", "enqueued", "username",
    ):
        assert hasattr(fake_bambuddy, name), name
    item = await _enqueue(fake_bambuddy)
    fake_bambuddy.set_status(item.id, "completed")
    assert item.id not in [i.id for i in await fake_bambuddy.list_queue()]
    assert (await fake_bambuddy.get_queue_item(item.id)).status == "completed"


async def test_inactive_printer_listed(fake_bambuddy):
    fake_bambuddy.add_printer("Printer 4", "A1", active=False)
    printers = await fake_bambuddy.list_printers()
    four = [p for p in printers if p.name == "Printer 4"]
    assert len(four) == 1 and four[0].active is False
    assert all(p.active for p in printers if p.name != "Printer 4")
