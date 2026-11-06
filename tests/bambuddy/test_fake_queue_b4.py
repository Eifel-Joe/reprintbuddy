import pytest

from reprintbuddy.bambuddy.client import (
    BambuddyRejected,
    BambuddyUnavailable,
    BatchRefLost,
)
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import EnqueueRequest


def _req(batch, *, top=False, pos=None, model="A1"):
    return EnqueueRequest(1, 1, model, None, batch, top, pos)


async def test_enqueue_appends_and_inserts(fake_bambuddy):
    fake = fake_bambuddy
    fake.add_foreign_item("F1")
    fake.add_foreign_item("F2")
    batch = await fake.ensure_batch("Template A", "r1", 1)
    first = await fake.enqueue(_req(batch))
    assert first.position == 3
    assert first.status == "pending"
    assert first.manual_start is False
    assert first.created_by == "reprintbuddy"
    assert first.label == "Template A.gcode.3mf / plate 1"
    top = await fake.enqueue(_req(batch, top=True))
    assert top.position == 1
    by_id = {i.id: i.position for i in await fake.list_queue()}
    assert by_id[top.id] == 1
    assert by_id[first.id] == 4
    mid = await fake.enqueue(_req(batch, pos=2))
    assert mid.position == 2
    by_label = {i.label: i.position for i in await fake.list_queue()}
    assert by_label["F1"] == 3 and by_label["F2"] == 4
    assert len(fake.enqueued) == 3
    assert fake.enqueued[-1] == _req(batch, pos=2)


async def test_insert_position_capped(fake_bambuddy):
    fake = fake_bambuddy
    fake.add_foreign_item("F1")
    fake.add_foreign_item("F2")
    batch = await fake.ensure_batch("Template A", "r1", 1)
    assert (await fake.enqueue(_req(batch, pos=99))).position == 3


async def test_list_queue_open_only(fake_bambuddy):
    fake = fake_bambuddy
    batch = await fake.ensure_batch("Template A", "r1", 1)
    item = await fake.enqueue(_req(batch))
    fake.set_status(item.id, "completed")
    assert await fake.list_queue() == []
    got = await fake.get_queue_item(item.id)
    assert got.status == "completed" and got.completed_at is not None
    found = await fake.find_items_by_batch(batch)
    assert [i.id for i in found] == [item.id]
    assert found[0].completed_at is not None


async def test_deleted_item_is_none(fake_bambuddy):
    fake = fake_bambuddy
    batch = await fake.ensure_batch("Template A", "r1", 1)
    item = await fake.enqueue(_req(batch))
    fake.delete_item(item.id)
    assert await fake.get_queue_item(item.id) is None
    assert await fake.find_items_by_batch(batch) == []


async def test_batch_ref_reused_and_lost(fake_bambuddy):
    fake = fake_bambuddy
    batch = await fake.ensure_batch("Template A", "r1", 1)
    await fake.enqueue(_req(batch))
    assert await fake.ensure_batch("Template A", "r1", 1) == batch
    other = await fake.ensure_batch("Template A", "r2", 1)
    assert other != batch
    with pytest.raises(BatchRefLost):
        await fake.ensure_batch("Template A", "r2", 1)


async def test_lost_response_effect_happens(fake_bambuddy):
    fake = fake_bambuddy
    batch = await fake.ensure_batch("Template A", "r1", 1)
    fake.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)
    with pytest.raises(BambuddyUnavailable):
        await fake.enqueue(_req(batch))
    assert len(await fake.find_items_by_batch(batch)) == 1
    await fake.enqueue(_req(batch))
    assert len(await fake.find_items_by_batch(batch)) == 2


async def test_fail_next_without_effect(fake_bambuddy):
    fake = fake_bambuddy
    batch = await fake.ensure_batch("Template A", "r1", 1)
    fake.fail_next("enqueue", BambuddyRejected("invalid", status=422))
    with pytest.raises(BambuddyRejected):
        await fake.enqueue(_req(batch))
    assert await fake.find_items_by_batch(batch) == []
    assert fake.enqueued == []


async def test_update_file(fake_bambuddy):
    fake_bambuddy.update_file(1, fingerprint="v2")
    assert (await fake_bambuddy.get_file(1)).fingerprint == "v2"


async def test_enqueue_rejects_unknown_model():
    fake = FakeBambuddy()
    fake.add_file("Template A.gcode.3mf")
    fake.add_printer("Printer 1", "A1")
    batch = await fake.ensure_batch("Template A", "r1", 1)
    with pytest.raises(BambuddyRejected) as exc:
        await fake.enqueue(_req(batch, model="X1"))
    assert exc.value.status == 400
