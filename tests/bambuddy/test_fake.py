import inspect
from datetime import UTC, datetime

import pytest

from reprintbuddy.bambuddy.client import (
    BambuddyClient,
    BambuddyRejected,
    BambuddyUnavailable,
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
        "plate_thumbnail", "filament_requirements", "enqueue",
        "get_queue_item", "list_queue", "list_all_items", "list_spools",
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


# ---- queue, spools (Task 7; single entries since v0.3.5, Spec 7.6) ------------------
def _ready() -> tuple[FakeBambuddy, int]:
    """A fake with one A1 printer and one file; returns it and the file id."""
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1")
    return fake, fake.add_file("Template A.gcode.3mf").id


def _req(file_id: int, **kw) -> EnqueueRequest:
    fields = dict(
        library_file_id=file_id, plate_id=1, target_model="A1", printer_id=None,
        insert_at_top=False, insert_position=None,
    )
    fields.update(kw)
    return EnqueueRequest(**fields)


async def test_enqueue_appends_and_inserts():
    fake, fid = _ready()
    f1, f2 = fake.add_foreign_item("F1"), fake.add_foreign_item("F2")
    assert (f1.position, f2.position) == (1, 2)
    last = await fake.enqueue(_req(fid))
    assert last.position == 3
    assert (last.status, last.manual_start, last.created_by) == ("pending", False, "reprintbuddy")
    assert last.label == "Template A.gcode.3mf / plate 1"
    top = await fake.enqueue(_req(fid, insert_at_top=True))
    assert top.position == 1
    assert [i.position for i in await fake.list_queue()] == [1, 2, 3, 4]
    assert (await fake.get_queue_item(f1.id)).position == 2
    second = await fake.enqueue(_req(fid, insert_position=2))
    assert second.position == 2
    assert (await fake.get_queue_item(f1.id)).position == 3
    assert fake.enqueued[-1] == _req(fid, insert_position=2)


async def test_enqueue_makes_a_single_entry_without_batch():
    """Spec 7.6 (v0.3.5): one entry per print, never a batch."""
    fake, fid = _ready()
    item = await fake.enqueue(_req(fid))
    assert item.batch_id is None
    assert not hasattr(fake, "ensure_batch")
    assert not hasattr(fake, "find_items_by_batch")


async def test_enqueue_stamps_creator_and_time_from_the_fake_clock():
    fake, fid = _ready()
    fake.now = lambda: datetime(2026, 11, 7, 8, 30)
    item = await fake.enqueue(_req(fid, plate_id=1))
    assert (item.created_by, item.created_at) == ("reprintbuddy", datetime(2026, 11, 7, 8, 30))
    assert (item.library_file_id, item.plate_id) == (fid, 1)
    fake.username = "robot"
    assert (await fake.enqueue(_req(fid))).created_by == "robot"


async def test_fake_clock_is_naive_utc_by_default():
    before = datetime.now(UTC).replace(tzinfo=None)
    stamp = FakeBambuddy().now()
    assert stamp.tzinfo is None
    assert before <= stamp <= datetime.now(UTC).replace(tzinfo=None)


async def test_foreign_item_for_a_file_and_time():
    fake, fid = _ready()
    item = fake.add_foreign_item(
        "Old", created_by=fake.username, library_file_id=fid, plate_id=1,
        created_at=datetime(2026, 11, 7, 8, 0),
    )
    assert (item.created_by, item.library_file_id, item.plate_id, item.created_at) == (
        "reprintbuddy", fid, 1, datetime(2026, 11, 7, 8, 0)
    )
    fake.now = lambda: datetime(2026, 11, 7, 9, 0)
    assert fake.add_foreign_item("Now").created_at == datetime(2026, 11, 7, 9, 0)
    assert fake.add_foreign_item("Undated", undated=True).created_at is None


async def test_list_all_items_includes_history_in_queue_order():
    fake, fid = _ready()
    first = await fake.enqueue(_req(fid))
    top = await fake.enqueue(_req(fid, insert_at_top=True))
    done = await fake.enqueue(_req(fid))
    fake.set_status(done.id, "completed")
    foreign = fake.add_foreign_item("F1", status="cancelled")
    fake.calls.clear()
    items = await fake.list_all_items()
    assert fake.calls == ["list_all_items"]
    assert {i.id for i in items} == {first.id, top.id, done.id, foreign.id}
    # Bambuddy sorts by (position, id); finished entries keep their old position.
    assert [i.id for i in items] == [
        i.id for i in sorted(items, key=lambda i: (i.position or 0, i.id))
    ]


async def test_insert_position_capped():
    fake, fid = _ready()
    fake.add_foreign_item("F1")
    fake.add_foreign_item("F2")
    assert (await fake.enqueue(_req(fid, insert_position=99))).position == 3


async def test_gaps_remain_after_delete():
    fake, fid = _ready()
    a, b = fake.add_foreign_item("A"), fake.add_foreign_item("B")
    fake.delete_item(a.id)
    assert (await fake.get_queue_item(b.id)).position == 2
    assert (await fake.enqueue(_req(fid))).position == 3


async def test_list_queue_open_only():
    fake, fid = _ready()
    item = await fake.enqueue(_req(fid))
    fake.set_status(item.id, "completed")
    assert await fake.list_queue() == []
    got = await fake.get_queue_item(item.id)
    assert got.status == "completed" and got.completed_at is not None
    assert [i.id for i in await fake.list_all_items()] == [item.id]


async def test_set_status_printing_sets_started_and_printer():
    fake, fid = _ready()
    item = await fake.enqueue(_req(fid))
    fake.set_status(item.id, "printing", printer_id=1)
    got = await fake.get_queue_item(item.id)
    assert got.started_at is not None and got.completed_at is None
    assert (got.printer_id, got.printer_name) == (1, "Printer 1")
    assert [i.id for i in await fake.list_queue()] == [item.id]


async def test_deleted_item_is_none():
    fake, fid = _ready()
    item = await fake.enqueue(_req(fid))
    fake.delete_item(item.id)
    assert await fake.get_queue_item(item.id) is None
    assert await fake.list_all_items() == []


async def test_lost_response_effect_happens():
    fake, fid = _ready()
    fake.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)
    with pytest.raises(BambuddyUnavailable):
        await fake.enqueue(_req(fid))
    assert len(await fake.list_all_items()) == 1
    await fake.enqueue(_req(fid))
    assert len(await fake.list_all_items()) == 2


async def test_fail_next_without_effect():
    fake, fid = _ready()
    fake.fail_next("enqueue", BambuddyRejected("invalid", status=422))
    with pytest.raises(BambuddyRejected):
        await fake.enqueue(_req(fid))
    assert await fake.list_all_items() == []
    assert fake.enqueued == []


@pytest.mark.parametrize(
    "method, args",
    [
        ("list_folders", ()), ("list_files", (None,)), ("get_file", (1,)),
        ("list_printers", ()), ("spool_mode", ()), ("list_spools", ()),
        ("get_queue_item", (1,)), ("list_queue", ()), ("list_all_items", ()),
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
    for gone in ("ensure_batch", "find_items_by_batch"):
        with pytest.raises(ValueError):
            FakeBambuddy().fail_next(gone, RuntimeError())


async def test_unavailable_applies_to_queue_and_spools():
    fake, fid = _ready()
    fake.unavailable = True
    calls = (fake.list_queue(), fake.list_spools(), fake.list_all_items(), fake.enqueue(_req(fid)))
    for call in calls:
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
    with pytest.raises(BambuddyRejected) as err:
        await fake.enqueue(_req(fid, target_model="X1"))
    assert err.value.status == 400
    assert err.value.detail == "No active printers for model: X1"
    assert await fake.list_all_items() == []


async def test_enqueue_ignores_inactive_printers():
    fake = FakeBambuddy()
    fake.add_printer("Printer 1", "A1", active=False)
    fid = fake.add_file("Template A.gcode.3mf").id
    with pytest.raises(BambuddyRejected) as err:
        await fake.enqueue(_req(fid))
    assert err.value.status == 400


async def test_enqueue_unknown_file_or_printer():
    fake, fid = _ready()
    with pytest.raises(BambuddyRejected) as err:
        await fake.enqueue(_req(99))
    assert err.value.status == 404
    with pytest.raises(BambuddyRejected) as err:
        await fake.enqueue(_req(fid, target_model=None, printer_id=99))
    assert err.value.status == 400


async def test_enqueue_to_printer_names_it():
    fake, fid = _ready()
    item = await fake.enqueue(_req(fid, target_model=None, printer_id=1))
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
    fake.fail_next("enqueue", BambuddyUnavailable("must stay queued"))
    with pytest.raises(ValueError):
        await fake.enqueue(_req(fid, **changes))
    assert fake.enqueued == [] and "enqueue" not in fake.calls
    with pytest.raises(BambuddyUnavailable):  # the injected failure was not used up
        await fake.enqueue(_req(fid))


async def test_spools_builtin():
    fake = FakeBambuddy()
    kept = fake.add_spool("PLA", remaining_g=250.0)
    archived = fake.add_spool("PLA", archived=True)
    assert await fake.list_spools() == [kept, archived]  # archived ones too, flagged (as the client asks)
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
    assert [s.archived for s in await fake.list_spools()] == [True]


def test_fake_matches_http_client_signatures():
    names = [n for n in dir(BambuddyClient) if not n.startswith("_")]
    for name in names:
        def params(cls):
            sig = inspect.signature(getattr(cls, name))
            return [(p.name, p.kind) for p in sig.parameters.values()]
        assert params(FakeBambuddy) == params(HttpBambuddyClient), name


async def test_insert_position_wins_over_insert_at_top():
    fake, fid = _ready()
    fake.add_foreign_item("F1")
    fake.add_foreign_item("F2")
    item = await fake.enqueue(_req(fid, insert_at_top=True, insert_position=2))
    assert item.position == 2
    item = await fake.enqueue(_req(fid, insert_at_top=True, insert_position=99))
    assert item.position == 4


async def test_foreign_item_not_pending_has_int_position():
    fake = FakeBambuddy()
    done = fake.add_foreign_item("Done", status="completed")
    assert done.position == 0
