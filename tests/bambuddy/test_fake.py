import inspect

import pytest

from reprintbuddy.bambuddy.client import (
    BambuddyClient,
    BambuddyRejected,
    BambuddyUnavailable,
    BatchRefLost,
    HttpBambuddyClient,
)
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import EnqueueRequest, FilamentRequirement, Printer


def test_fake_is_client():
    assert isinstance(FakeBambuddy(), BambuddyClient)


def test_protocol_matches_master_plan():
    names = {n for n in dir(BambuddyClient) if not n.startswith("_")}
    assert names == {
        "ping", "list_folders", "list_files", "get_file", "list_plates",
        "plate_thumbnail", "filament_requirements", "ensure_batch", "enqueue",
        "get_queue_item", "list_queue", "find_items_by_batch", "list_spools",
        "list_printers", "spool_mode", "file_thumbnail",
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


async def test_spool_mode_follows_spoolman_enabled():
    fake = FakeBambuddy()
    assert await fake.spool_mode() == "builtin"
    fake.spoolman_enabled = True
    assert await fake.spool_mode() == "spoolman"
    fake.unavailable = True
    with pytest.raises(BambuddyUnavailable):
        await fake.spool_mode()


# ---- queue, batches, spools (Task 7) ------------------------------------------------
def _ready() -> tuple[FakeBambuddy, int]:
    """A fake with one A1 printer and one file; returns it and the file id."""
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    return fake, fake.add_file("Template A.gcode.3mf").id


def _req(file_id: int, batch_id: int, **kw) -> EnqueueRequest:
    fields = dict(
        library_file_id=file_id, plate_id=1, target_model="A1", printer_id=None,
        batch_id=batch_id, insert_at_top=False, insert_position=None,
    )
    fields.update(kw)
    return EnqueueRequest(**fields)


async def test_enqueue_appends_and_inserts():
    fake, fid = _ready()
    f1, f2 = fake.add_foreign_item("F1"), fake.add_foreign_item("F2")
    assert (f1.position, f2.position) == (1, 2)
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    last = await fake.enqueue(_req(fid, batch))
    assert last.position == 3
    assert (last.status, last.manual_start, last.created_by) == ("pending", False, "reprintbuddy")
    assert last.label == "Template A.gcode.3mf / plate 1"
    top = await fake.enqueue(_req(fid, batch, insert_at_top=True))
    assert top.position == 1
    assert [i.position for i in await fake.list_queue()] == [1, 2, 3, 4]
    assert (await fake.get_queue_item(f1.id)).position == 2
    second = await fake.enqueue(_req(fid, batch, insert_position=2))
    assert second.position == 2
    assert (await fake.get_queue_item(f1.id)).position == 3
    assert fake.enqueued[-1] == _req(fid, batch, insert_position=2)


async def test_insert_position_capped():
    fake, fid = _ready()
    fake.add_foreign_item("F1")
    fake.add_foreign_item("F2")
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    assert (await fake.enqueue(_req(fid, batch, insert_position=99))).position == 3


async def test_gaps_remain_after_delete():
    fake, fid = _ready()
    a, b = fake.add_foreign_item("A"), fake.add_foreign_item("B")
    fake.delete_item(a.id)
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    assert (await fake.get_queue_item(b.id)).position == 2
    assert (await fake.enqueue(_req(fid, batch))).position == 3


async def test_list_queue_open_only():
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    item = await fake.enqueue(_req(fid, batch))
    fake.set_status(item.id, "completed")
    assert await fake.list_queue() == []
    got = await fake.get_queue_item(item.id)
    assert got.status == "completed" and got.completed_at is not None
    assert [i.id for i in await fake.find_items_by_batch(batch)] == [item.id]


async def test_set_status_printing_sets_started_and_printer():
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    item = await fake.enqueue(_req(fid, batch))
    fake.set_status(item.id, "printing", printer_id=1)
    got = await fake.get_queue_item(item.id)
    assert got.started_at is not None and got.completed_at is None
    assert (got.printer_id, got.printer_name) == (1, "Printer 1")
    assert [i.id for i in await fake.list_queue()] == [item.id]


async def test_deleted_item_is_none():
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    item = await fake.enqueue(_req(fid, batch))
    fake.delete_item(item.id)
    assert await fake.get_queue_item(item.id) is None
    assert await fake.find_items_by_batch(batch) == []


async def test_batch_ref_reused_and_lost():
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    assert batch == 1
    # an empty batch behind a known reference: the batch list omits it -> lost
    with pytest.raises(BatchRefLost):
        await fake.ensure_batch("Job", "ref-1", fid)
    await fake.enqueue(_req(fid, batch))
    assert await fake.ensure_batch("Job", "ref-1", fid) == batch
    assert await fake.ensure_batch("Job", "ref-2", fid) == 2


async def test_ensure_batch_unknown_file():
    fake = FakeBambuddy()
    with pytest.raises(BambuddyRejected) as err:
        await fake.ensure_batch("Job", "ref-1", 99)
    assert (err.value.status, err.value.detail) == (404, "Library file not found")


async def test_lost_response_effect_happens():
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    fake.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)
    with pytest.raises(BambuddyUnavailable):
        await fake.enqueue(_req(fid, batch))
    assert len(await fake.find_items_by_batch(batch)) == 1
    await fake.enqueue(_req(fid, batch))
    assert len(await fake.find_items_by_batch(batch)) == 2


async def test_lost_response_of_ensure_batch_effect_happens():
    fake, fid = _ready()
    fake.fail_next("ensure_batch", BambuddyUnavailable("timeout"), after_effect=True)
    with pytest.raises(BambuddyUnavailable):
        await fake.ensure_batch("Job", "ref-1", fid)
    # the batch exists but is empty -> exactly the case of Decision 10
    with pytest.raises(BatchRefLost):
        await fake.ensure_batch("Job", "ref-1", fid)


async def test_fail_next_without_effect():
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    fake.fail_next("enqueue", BambuddyRejected("invalid", status=422))
    with pytest.raises(BambuddyRejected):
        await fake.enqueue(_req(fid, batch))
    assert await fake.find_items_by_batch(batch) == []
    assert fake.enqueued == []


@pytest.mark.parametrize(
    "method, args",
    [
        ("list_folders", ()), ("list_files", (None,)), ("get_file", (1,)),
        ("list_printers", ()), ("spool_mode", ()), ("list_spools", ()),
        ("get_queue_item", (1,)), ("list_queue", ()), ("find_items_by_batch", (1,)),
    ],
)
@pytest.mark.parametrize("after_effect", [False, True])
async def test_fail_next_works_for_read_methods(method, args, after_effect):
    fake = FakeBambuddy()
    fake.fail_next(method, BambuddyUnavailable("boom"), after_effect=after_effect)
    with pytest.raises(BambuddyUnavailable):
        await getattr(fake, method)(*args)
    await getattr(fake, method)(*args)  # only the next call fails


def test_fail_next_unknown_method():
    with pytest.raises(ValueError):
        FakeBambuddy().fail_next("nope", RuntimeError())


async def test_unavailable_applies_to_queue_and_spools():
    fake, fid = _ready()
    fake.unavailable = True
    for call in (fake.list_queue(), fake.list_spools(), fake.ensure_batch("J", "r", fid)):
        with pytest.raises(BambuddyUnavailable):
            await call


async def test_update_file():
    fake, fid = _ready()
    fake.update_file(fid, fingerprint="v2")
    assert (await fake.get_file(fid)).fingerprint == "v2"
    fake.update_file(fid, filename="Template B.gcode.3mf")
    got = await fake.get_file(fid)
    assert (got.filename, got.fingerprint) == ("Template B.gcode.3mf", "v2")


async def test_enqueue_rejects_unknown_model():
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    fid = fake.add_file("Template A.gcode.3mf").id
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    with pytest.raises(BambuddyRejected) as err:
        await fake.enqueue(_req(fid, batch, target_model="X1"))
    assert err.value.status == 400
    assert err.value.detail == "No active printers for model: X1"
    assert await fake.find_items_by_batch(batch) == []


async def test_enqueue_ignores_inactive_printers():
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1", active=False)
    fid = fake.add_file("Template A.gcode.3mf").id
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    with pytest.raises(BambuddyRejected) as err:
        await fake.enqueue(_req(fid, batch))
    assert err.value.status == 400


async def test_enqueue_unknown_file_or_batch_or_printer():
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    for req in (_req(99, batch), _req(fid, 99)):
        with pytest.raises(BambuddyRejected) as err:
            await fake.enqueue(req)
        assert err.value.status == 404
    with pytest.raises(BambuddyRejected) as err:
        await fake.enqueue(_req(fid, batch, target_model=None, printer_id=99))
    assert err.value.status == 400


async def test_enqueue_to_printer_names_it():
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    item = await fake.enqueue(_req(fid, batch, target_model=None, printer_id=1))
    assert (item.printer_id, item.printer_name, item.target_model) == (1, "Printer 1", None)


@pytest.mark.parametrize(
    "changes",
    [
        {"target_model": ""},
        {"target_model": "A1", "printer_id": 1},
        {"target_model": None, "printer_id": None},
        {"insert_position": 0},
    ],
)
async def test_enqueue_value_errors_before_any_effect(changes):
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    fake.fail_next("enqueue", BambuddyUnavailable("must stay queued"))
    with pytest.raises(ValueError):
        await fake.enqueue(_req(fid, batch, **changes))
    assert fake.enqueued == [] and "enqueue" not in fake.calls
    with pytest.raises(BambuddyUnavailable):  # the injected failure was not used up
        await fake.enqueue(_req(fid, batch))


async def test_spools_builtin():
    fake = FakeBambuddy()
    kept = fake.add_spool("PLA", remaining_g=250.0)
    fake.add_spool("PLA", archived=True)
    assert await fake.list_spools() == [kept]
    assert kept.remaining_g == 250.0


async def test_spools_follow_mode():
    fake = FakeBambuddy()
    own = fake.add_spool("PLA", in_mode="builtin")
    spoolman = fake.add_spool("PLA", in_mode="spoolman")
    fake.spoolman_enabled = False
    assert await fake.list_spools() == [own]
    fake.spoolman_enabled = True
    assert await fake.list_spools() == [spoolman]


async def test_spoolman_unreachable_no_fallback():
    fake = FakeBambuddy()
    fake.add_spool("PLA", in_mode="builtin")
    fake.spoolman_enabled = True
    fake.spoolman_reachable = False
    with pytest.raises(BambuddyUnavailable, match="Spoolman server is not reachable"):
        await fake.list_spools()
    fake.spoolman_enabled = False  # an unreachable Spoolman does not matter in the own mode
    assert len(await fake.list_spools()) == 1


async def test_list_spools_records_only_its_own_call():
    fake = FakeBambuddy()
    await fake.list_spools()
    assert fake.calls == ["list_spools"]


async def test_add_spool_color_name_and_created_at():
    from datetime import datetime

    fake = FakeBambuddy()
    spool = fake.add_spool("PLA", color_name="Black", created_at=datetime(2026, 11, 1))
    assert (spool.color_name, spool.created_at) == ("Black", datetime(2026, 11, 1))
    fake.set_remaining(spool.id, 10.0)
    assert (await fake.list_spools())[0].remaining_g == 10.0
    fake.archive_spool(spool.id)
    assert await fake.list_spools() == []


def test_fake_matches_http_client_signatures():
    names = [n for n in dir(BambuddyClient) if not n.startswith("_")]
    for name in names:
        def params(cls):
            sig = inspect.signature(getattr(cls, name))
            return [(p.name, p.kind) for p in sig.parameters.values()]
        assert params(FakeBambuddy) == params(HttpBambuddyClient), name


async def test_find_items_by_batch_sorted_by_position():
    fake, fid = _ready()
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    first = await fake.enqueue(_req(fid, batch))
    top = await fake.enqueue(_req(fid, batch, insert_at_top=True))
    assert first.id < top.id
    assert [i.id for i in await fake.find_items_by_batch(batch)] == [top.id, first.id]


async def test_insert_position_wins_over_insert_at_top():
    fake, fid = _ready()
    fake.add_foreign_item("F1")
    fake.add_foreign_item("F2")
    batch = await fake.ensure_batch("Job", "ref-1", fid)
    item = await fake.enqueue(_req(fid, batch, insert_at_top=True, insert_position=2))
    assert item.position == 2
    item = await fake.enqueue(_req(fid, batch, insert_at_top=True, insert_position=99))
    assert item.position == 4


async def test_ensure_batch_empty_name_rejected():
    fake, fid = _ready()
    for name in ("", "  "):
        with pytest.raises(BambuddyRejected) as err:
            await fake.ensure_batch(name, "ref-1", fid)
        assert (err.value.status, err.value.detail) == (400, "Batch name is required")


async def test_batch_ref_lost_text_is_bambuddys():
    fake, fid = _ready()
    await fake.ensure_batch("Job", "ref-1", fid)
    with pytest.raises(BatchRefLost) as err:
        await fake.ensure_batch("Job", "ref-1", fid)
    assert err.value.detail == "A batch for this external_source and external_ref already exists"


async def test_foreign_item_not_pending_has_int_position():
    fake = FakeBambuddy()
    done = fake.add_foreign_item("Done", status="completed")
    assert done.position == 0
