import asyncio
import time

import pytest

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import EnqueueRequest


async def _enqueue(fake: FakeBambuddy):
    batch = await fake.ensure_batch("Template A", "ref-1", 1)
    return await fake.enqueue(EnqueueRequest(1, 1, "A1", None, batch, False, None))


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


async def test_fail_next_on_read_method(fake_bambuddy):
    fake_bambuddy.fail_next("get_file", BambuddyUnavailable("timeout"))
    with pytest.raises(BambuddyUnavailable):
        await fake_bambuddy.get_file(1)
    assert (await fake_bambuddy.get_file(1)) is not None
    fake_bambuddy.fail_next("list_printers", BambuddyRejected("Forbidden", status=403))
    with pytest.raises(BambuddyRejected) as exc:
        await fake_bambuddy.list_printers()
    assert exc.value.status == 403
    assert len(await fake_bambuddy.list_printers()) == 3


def test_fail_next_unknown_method_rejected(fake_bambuddy):
    with pytest.raises(ValueError):
        fake_bambuddy.fail_next("no_such_method", BambuddyUnavailable("x"))
    with pytest.raises(ValueError):
        fake_bambuddy.fail_next("ping", BambuddyUnavailable("x"))
