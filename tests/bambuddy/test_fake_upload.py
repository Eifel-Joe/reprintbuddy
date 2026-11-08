"""The stand-in uploads like Bambuddy (`library.py:2509-2677`; v0.3.9 p20)."""

import io
import zipfile
from datetime import datetime

import pytest

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import UploadedFile


def zip_bytes(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"data of " + name.encode())
    return buffer.getvalue()


SLICED = zip_bytes("3D/3dmodel.model", "Metadata/plate_1.gcode")
PROJECT = zip_bytes("3D/3dmodel.model")


async def test_upload_lands_in_the_folder_and_is_listed():
    fake = FakeBambuddy()
    fake.now = lambda: datetime(2026, 11, 8, 12, 0, 0)
    folder = fake.add_folder("Templates")
    result = await fake.upload_file(folder.id, "A.gcode.3mf", io.BytesIO(SLICED))
    assert result == UploadedFile(id=result.id, filename="A.gcode.3mf", sliced=True, duplicate_of=None)
    [listed] = await fake.list_files(folder.id)
    assert (listed.id, listed.filename, listed.sliced) == (result.id, "A.gcode.3mf", True)
    assert (listed.created_by, listed.size, listed.created_at) == ("reprintbuddy", len(SLICED), datetime(2026, 11, 8, 12))
    assert await fake.list_plates(result.id)  # a sliced file has a plate
    assert fake.uploaded == [(folder.id, "A.gcode.3mf", SLICED)]
    assert "upload_file" in fake.calls


async def test_upload_to_the_top_level():
    fake = FakeBambuddy()
    result = await fake.upload_file(None, "A.3mf", io.BytesIO(SLICED))
    assert [f.id for f in await fake.list_files(None)] == [result.id]


async def test_bambuddy_judges_by_content_not_name():
    fake = FakeBambuddy()
    assert (await fake.upload_file(None, "A.3mf", io.BytesIO(SLICED))).sliced is True
    assert (await fake.upload_file(None, "B.gcode.3mf", io.BytesIO(PROJECT))).sliced is False


async def test_same_name_is_accepted_like_bambuddy():
    fake = FakeBambuddy()
    await fake.upload_file(None, "A.3mf", io.BytesIO(SLICED))
    await fake.upload_file(None, "A.3mf", io.BytesIO(PROJECT))
    assert [f.filename for f in await fake.list_files(None)] == ["A.3mf", "A.3mf"]


async def test_same_content_is_reported_as_duplicate():
    fake = FakeBambuddy()
    first = await fake.upload_file(None, "A.3mf", io.BytesIO(SLICED))
    second = await fake.upload_file(None, "B.3mf", io.BytesIO(SLICED))
    assert second.duplicate_of == first.id


async def test_without_the_right_bambuddy_refuses():
    fake = FakeBambuddy()
    fake.can_upload = False
    with pytest.raises(BambuddyRejected) as caught:
        await fake.upload_file(None, "A.3mf", io.BytesIO(SLICED))
    assert caught.value.status == 403
    assert "can_manage_library" in caught.value.detail
    assert fake.uploaded == []


async def test_unknown_folder_and_read_only_external_folder():
    fake = FakeBambuddy()
    with pytest.raises(BambuddyRejected) as caught:
        await fake.upload_file(99, "A.3mf", io.BytesIO(SLICED))
    assert caught.value.status == 404
    mounted = fake.add_folder("Mounted", external=True, readonly=True)
    with pytest.raises(BambuddyRejected) as caught:
        await fake.upload_file(mounted.id, "A.3mf", io.BytesIO(SLICED))
    assert caught.value.status == 403
    assert fake.uploaded == []


async def test_writable_external_folder_is_accepted_like_bambuddy():
    """Reprintbuddy never sends there (Spec 5.1a); Bambuddy itself would accept it."""
    fake = FakeBambuddy()
    mounted = fake.add_folder("Mounted", external=True)
    assert (await fake.list_folders())[0].is_external is True
    await fake.upload_file(mounted.id, "A.3mf", io.BytesIO(SLICED))
    assert len(fake.uploaded) == 1


async def test_a_lost_answer_leaves_the_file_in_bambuddy():
    fake = FakeBambuddy()
    fake.fail_next("upload_file", BambuddyUnavailable("timeout"), after_effect=True)
    with pytest.raises(BambuddyUnavailable):
        await fake.upload_file(None, "A.3mf", io.BytesIO(SLICED))
    assert [f.filename for f in await fake.list_files(None)] == ["A.3mf"]


async def test_a_failure_before_the_effect_leaves_nothing():
    fake = FakeBambuddy()
    fake.fail_next("upload_file", BambuddyUnavailable("down"))
    with pytest.raises(BambuddyUnavailable):
        await fake.upload_file(None, "A.3mf", io.BytesIO(SLICED))
    assert await fake.list_files(None) == []


async def test_switched_off():
    fake = FakeBambuddy()
    fake.unavailable = True
    with pytest.raises(BambuddyUnavailable):
        await fake.upload_file(None, "A.3mf", io.BytesIO(SLICED))
    assert fake.uploaded == []
