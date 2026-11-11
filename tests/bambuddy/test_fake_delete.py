"""The stand-in deletes like Bambuddy (`print_queue.py:2697-2748`; Spec 7.2a, 0.4.1 p382)."""

import pytest

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import EnqueueRequest


async def _own(fake: FakeBambuddy) -> int:
    return (await fake.enqueue(EnqueueRequest(1, 1, "A1", None, False, None))).id


@pytest.fixture
def fake():
    fake = FakeBambuddy()
    fake.add_file("Template A.gcode.3mf", printer_model="A1")
    fake.add_printer("Printer 1", "A1")
    return fake


async def test_a_waiting_own_entry_is_deleted(fake):
    item_id = await _own(fake)
    assert await fake.delete_queue_item(item_id) == "deleted"
    assert await fake.get_queue_item(item_id) is None
    assert fake.deleted == [item_id]
    assert fake.calls[-2:] == ["delete_queue_item", "get_queue_item"]


async def test_unknown_entry_is_gone(fake):
    assert await fake.delete_queue_item(99) == "gone"
    assert fake.deleted == []


async def test_a_printing_entry_is_refused(fake):
    item_id = await _own(fake)
    fake.set_status(item_id, "printing")
    with pytest.raises(BambuddyRejected) as info:
        await fake.delete_queue_item(item_id)
    assert (info.value.status, info.value.detail) == (400, "Cannot delete item that is currently printing")
    assert (await fake.get_queue_item(item_id)).status == "printing"


async def test_without_the_right_it_is_refused(fake):
    item_id = await _own(fake)
    fake.can_delete_own = False
    with pytest.raises(BambuddyRejected) as info:
        await fake.delete_queue_item(item_id)
    assert info.value.status == 403
    assert "queue:delete_own" in info.value.detail
    assert await fake.get_queue_item(item_id) is not None


async def test_an_entry_of_somebody_else_is_refused(fake):
    item = fake.add_foreign_item()
    with pytest.raises(BambuddyRejected) as info:
        await fake.delete_queue_item(item.id)
    assert (info.value.status, info.value.detail) == (403, "You can only delete your own queue items")


async def test_a_lost_answer_after_the_delete(fake):
    item_id = await _own(fake)
    fake.fail_next("delete_queue_item", BambuddyUnavailable("timeout"), after_effect=True)
    with pytest.raises(BambuddyUnavailable):
        await fake.delete_queue_item(item_id)
    assert await fake.get_queue_item(item_id) is None


async def test_a_failure_before_the_delete(fake):
    item_id = await _own(fake)
    fake.fail_next("delete_queue_item", BambuddyUnavailable("timeout"))
    with pytest.raises(BambuddyUnavailable):
        await fake.delete_queue_item(item_id)
    assert await fake.get_queue_item(item_id) is not None


async def test_switched_off(fake):
    item_id = await _own(fake)
    fake.unavailable = True
    with pytest.raises(BambuddyUnavailable):
        await fake.delete_queue_item(item_id)
    fake.unavailable = False
    assert await fake.get_queue_item(item_id) is not None
