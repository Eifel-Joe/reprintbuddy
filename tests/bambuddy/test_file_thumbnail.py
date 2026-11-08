"""The preview image of a library file and the file flags `has_thumbnail`, `sliced` (Task 11)."""

import pytest
from fixtures import API, KEY, client, load

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnexpected
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.parse import parse_file, parse_files
from reprintbuddy.bambuddy.types import LibraryFile

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF87 = b"GIF87a" + b"\x00" * 16
GIF89 = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x10\x00\x00\x00" + b"WEBP" + b"\x00" * 8


def _file(**over) -> dict:
    entry = dict(load("files_root.json")[0])
    entry.update(over)
    return entry


# ---- client -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content, content_type",
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF87, "image/gif"),
        (GIF89, "image/gif"),
        (WEBP, "image/webp"),
        (JPEG, "image/jpeg; charset=binary"),
    ],
)
async def test_file_thumbnail_returns_type_and_bytes(respx_mock, content, content_type):
    route = respx_mock.get(f"{API}/library/files/7/thumbnail")
    route.respond(200, content=content, headers={"content-type": content_type})
    kind, data = await client().file_thumbnail(7)
    assert (kind, data) == (content_type.split(";")[0], content)
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.url.path == "/api/v1/library/files/7/thumbnail"
    assert request.headers["X-API-Key"] == KEY
    assert KEY not in str(request.url)


async def test_file_thumbnail_404_is_rejected(respx_mock):
    respx_mock.get(f"{API}/library/files/7/thumbnail").respond(404, json={"detail": "Thumbnail not found"})
    with pytest.raises(BambuddyRejected) as err:
        await client().file_thumbnail(7)
    assert err.value.status == 404


@pytest.mark.parametrize(
    "content, content_type",
    [
        (b"<html>login</html>", "text/html"),
        (PNG, "text/html"),  # right bytes, wrong declared type
        (b"GIF89a....", "image/png"),  # declared PNG, no PNG head
        (PNG, "image/jpeg"),  # declared JPEG, PNG head
        (b"RIFF\x00\x00\x00\x00WAVE", "image/webp"),  # RIFF, but no WEBP
        (b"", "image/png"),
        (PNG, ""),  # no declared type at all
        (PNG, "image/svg+xml"),
    ],
)
async def test_file_thumbnail_wrong_type_or_magic_is_unexpected(respx_mock, content, content_type):
    respx_mock.get(f"{API}/library/files/7/thumbnail").respond(
        200, content=content, headers={"content-type": content_type}
    )
    with pytest.raises(BambuddyUnexpected):
        await client().file_thumbnail(7)


# ---- parser -------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [(None, False), ("", False), ("   ", False), ("thumbs/x.png", True)])
def test_has_thumbnail_from_thumbnail_path(value, expected):
    assert parse_file(_file(thumbnail_path=value)).has_thumbnail is expected
    assert parse_files([_file(thumbnail_path=value)])[0].has_thumbnail is expected


def test_has_thumbnail_missing_key_and_non_text():
    entry = _file()
    del entry["thumbnail_path"]
    assert parse_file(entry).has_thumbnail is False
    assert parse_file(_file(thumbnail_path=5)).has_thumbnail is False


@pytest.mark.parametrize(
    "file_type, filename, expected",
    [
        ("gcode.3mf", "Template A.gcode.3mf", True),
        ("gcode.3mf", "Odd name.3mf", True),  # Bambuddy judged by the zip content (#2993)
        ("3mf", "Template A.gcode.3mf", True),  # old entry before #1600
        ("3mf", "TEMPLATE A.GCODE.3MF", True),
        (None, "Template A.gcode.3mf", True),  # missing file_type follows the name
        (None, "Template A.3mf", False),
        ("3mf", "Template A.3mf", False),
        ("gcode", "Template A.gcode", False),
        ("gcode", "Template A.gcode.3mf", False),  # only None / 3mf fall back to the name
        ("stl", "Template A.stl", False),
    ],
)
def test_sliced_rule(file_type, filename, expected):
    entry = _file(file_type=file_type, filename=filename)
    assert parse_file(entry).sliced is expected
    assert parse_files([entry])[0].sliced is expected
    without = dict(entry)
    if file_type is None:
        del without["file_type"]
        assert parse_file(without).sliced is expected


# ---- defaults, fake -------------------------------------------------------------------


def test_new_fields_come_last_with_defaults():
    old = LibraryFile(1, "a.gcode.3mf", None, "v", "A1")
    assert old.has_thumbnail is False and old.sliced is True
    assert (old.created_by, old.size, old.created_at) == (None, None, None)
    names = [f for f in LibraryFile.__dataclass_fields__]
    # v0.3.9 p20 added creator, size and time after them, again with defaults.
    assert names[-5:] == ["has_thumbnail", "sliced", "created_by", "size", "created_at"]


async def test_fake_file_thumbnail():
    fake = FakeBambuddy()
    with_image = fake.add_file("A.gcode.3mf")
    without = fake.add_file("B.gcode.3mf", has_thumbnail=False)
    unsliced = fake.add_file("C.3mf", sliced=False)
    assert (with_image.has_thumbnail, with_image.sliced) == (True, True)
    assert (without.has_thumbnail, unsliced.sliced) == (False, False)
    kind, data = await fake.file_thumbnail(with_image.id)
    assert kind == "image/png" and data.startswith(b"\x89PNG")
    with pytest.raises(BambuddyRejected):
        await fake.file_thumbnail(without.id)
    with pytest.raises(BambuddyRejected):
        await fake.file_thumbnail(999)
    assert fake.calls.count("file_thumbnail") == 3
    fake.fail_next("file_thumbnail", BambuddyRejected("x"))
    with pytest.raises(BambuddyRejected):
        await fake.file_thumbnail(with_image.id)
    fake.unavailable = True
    with pytest.raises(Exception):
        await fake.file_thumbnail(with_image.id)
