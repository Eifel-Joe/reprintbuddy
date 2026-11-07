from datetime import datetime

import pytest

from reprintbuddy.bambuddy.client import BambuddyUnavailable
from reprintbuddy.bambuddy.fake import FakeBambuddy


async def test_add_spool_defaults_and_sequential_ids():
    fake = FakeBambuddy()
    first = fake.add_spool("PLA")
    second = fake.add_spool("PETG", brand="Other Brand", subtype="Matte", label_g=750)
    assert (first.id, second.id) == (1, 2)
    assert first.brand == "Example Brand"
    assert first.color_hex == "#000000"
    assert (first.label_g, first.remaining_g, first.archived) == (1000, 1000.0, False)
    assert (second.subtype, second.label_g) == ("Matte", 750)
    assert [s.id for s in await fake.list_spools()] == [1, 2]


async def test_color_name_and_created_at_passed():
    fake = FakeBambuddy()
    fake.add_spool("PLA", color_name="Black", created_at=datetime(2026, 11, 1))
    (spool,) = await fake.list_spools()
    assert spool.color_name == "Black"
    assert spool.created_at == datetime(2026, 11, 1)


async def test_set_remaining():
    fake = FakeBambuddy()
    spool = fake.add_spool("PLA")
    fake.set_remaining(spool.id, 250.0)
    assert (await fake.list_spools())[0].remaining_g == 250.0


async def test_archive_spool():
    fake = FakeBambuddy()
    keep = fake.add_spool("PLA")
    gone = fake.add_spool("PLA")
    fake.archive_spool(gone.id)
    assert [s.id for s in await fake.list_spools()] == [keep.id]


async def test_archived_spool_added_archived_is_excluded():
    fake = FakeBambuddy()
    fake.add_spool("PLA", archived=True)
    assert await fake.list_spools() == []


async def test_list_spools_unavailable():
    fake = FakeBambuddy()
    fake.add_spool("PLA")
    fake.unavailable = True
    with pytest.raises(BambuddyUnavailable):
        await fake.list_spools()
    assert fake.calls == ["list_spools"]


async def test_list_spools_fail_next():
    fake = FakeBambuddy()
    fake.fail_next("list_spools", BambuddyUnavailable("boom"))
    with pytest.raises(BambuddyUnavailable):
        await fake.list_spools()
    assert await fake.list_spools() == []
