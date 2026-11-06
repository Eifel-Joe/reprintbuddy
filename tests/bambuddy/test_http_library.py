"""Reading the library through `HttpBambuddyClient` (B2 Task 3, Decisions 1, 6, 7, 8)."""

import pytest
from fixtures import API, client, load

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnexpected
from reprintbuddy.bambuddy.parse import (
    normalize_color,
    parse_file,
    parse_files,
    parse_folders,
    parse_plates,
    parse_requirements,
)
from reprintbuddy.bambuddy.types import LibraryFile, LibraryFolder

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _folder(folder_id: int, name: str, parent_id: int | None, children: list | None = None) -> dict:
    return {"id": folder_id, "name": name, "parent_id": parent_id, "children": children or []}


def _templates_id() -> int:
    return next(f["id"] for f in load("folders.json") if f["name"] == "Templates")


# ---- folders (Decision 6) --------------------------------------------------------


async def test_list_folders_flattens_tree(respx_mock):
    respx_mock.get(f"{API}/library/folders/").respond(200, json=load("folders.json"))
    folders = await client().list_folders()

    expected: list[tuple] = []

    def walk(level):
        for entry in level:
            expected.append((entry["id"], entry["name"], entry["parent_id"]))
            walk(entry["children"])

    walk(load("folders.json"))
    assert [(f.id, f.name, f.parent_id) for f in folders] == expected
    assert len({f.id for f in folders}) == len(folders)  # each exactly once
    assert all(isinstance(f, LibraryFolder) for f in folders)
    # the sub-folder points at its parent
    child = next(f for f in folders if f.name == "Templates 2")
    assert child.parent_id == _templates_id()


async def test_list_folders_two_levels_deep(respx_mock):
    tree = [
        _folder(1, "A", None, [_folder(2, "B", 1, [_folder(3, "C", 2)]), _folder(4, "D", 1)]),
        _folder(5, "E", None),
    ]
    respx_mock.get(f"{API}/library/folders/").respond(200, json=tree)
    folders = await client().list_folders()
    assert [(f.id, f.parent_id) for f in folders] == [(1, None), (2, 1), (3, 2), (4, 1), (5, None)]


async def test_list_folders_empty(respx_mock):
    respx_mock.get(f"{API}/library/folders/").respond(200, json=[])
    assert await client().list_folders() == []


def test_child_without_parent_id_gets_the_enclosing_folder():
    tree = [{"id": 1, "name": "A", "parent_id": None, "children": [{"id": 2, "name": "B"}]}]
    assert [(f.id, f.parent_id) for f in parse_folders(tree)] == [(1, None), (2, 1)]


# ---- files (Decision 1, 7) -------------------------------------------------------


async def test_list_files_root(respx_mock):
    root = respx_mock.get(f"{API}/library/files/").respond(200, json=load("files_root.json"))
    files = await client().list_files(None)
    assert root.calls.last.request.url.query == b""  # no query parameter at the top level
    assert [f.id for f in files] == [e["id"] for e in load("files_root.json")]
    assert all(isinstance(f, LibraryFile) for f in files)


async def test_list_files_folder(respx_mock):
    folder_id = _templates_id()
    inside = respx_mock.get(f"{API}/library/files/", params={"folder_id": folder_id}).respond(
        200, json=load("files_folder.json")
    )
    files = await client().list_files(folder_id)
    assert inside.call_count == 1
    assert [f.id for f in files] == [e["id"] for e in load("files_folder.json")]
    first = files[0]
    assert first.filename == "Template A.gcode.3mf"
    assert first.printer_model == "A1"
    assert first.fingerprint is None  # the list has no file_hash (Decision 7)
    assert first.folder_id == folder_id


async def test_get_file_has_fingerprint(respx_mock):
    answer = load("file.json")
    respx_mock.get(f"{API}/library/files/{answer['id']}").respond(200, json=answer)
    file = await client().get_file(answer["id"])
    assert file.fingerprint == answer["file_hash"]
    assert file.printer_model == "A1"
    assert (file.id, file.filename, file.folder_id) == (answer["id"], answer["filename"], answer["folder_id"])


async def test_get_file_404_is_none(respx_mock):
    respx_mock.get(f"{API}/library/files/999").respond(404, json=load("file_404.json"))
    assert await client().get_file(999) is None


def test_file_optional_fields_may_be_missing():
    file = parse_file({"id": 3, "filename": "Template A.gcode.3mf"})
    assert (file.folder_id, file.fingerprint, file.printer_model) == (None, None, None)


def test_external_file_without_hash_has_no_fingerprint():
    file = parse_file({**load("file.json"), "file_hash": None, "is_external": True})
    assert file.fingerprint is None


# ---- plates -----------------------------------------------------------------------


async def test_plates(respx_mock):
    answer = load("plates.json")
    route = respx_mock.get(f"{API}/library/files/5/plates")
    route.respond(200, json=answer)
    plates = await client().list_plates(5)
    assert [p.index for p in plates] == [p["index"] for p in answer["plates"]]
    assert [p.name for p in plates] == [p["name"] for p in answer["plates"]]

    route.respond(200, json={"plates": []})
    assert await client().list_plates(5) == []

    route.respond(404, json=load("file_404.json"))
    with pytest.raises(BambuddyRejected) as err:
        await client().list_plates(5)
    assert err.value.status == 404


def test_plate_name_is_optional():
    plates = parse_plates({"plates": [{"index": 1}, {"index": 2, "name": None}]})
    assert [(p.index, p.name) for p in plates] == [(1, None), (2, None)]


# ---- thumbnail --------------------------------------------------------------------


async def test_thumbnail_png(respx_mock):
    route = respx_mock.get(f"{API}/library/files/5/plate-thumbnail/1")
    route.respond(200, content=PNG, headers={"content-type": "image/png"})
    assert await client().plate_thumbnail(5, 1) == PNG

    route.respond(404, json=load("plate_404.json"))
    with pytest.raises(BambuddyRejected) as err:
        await client().plate_thumbnail(5, 1)
    assert err.value.status == 404
    assert "not found" in err.value.detail


@pytest.mark.parametrize(
    "content, content_type",
    [
        (b"<html>login</html>", "text/html"),
        (PNG, "text/html"),  # right bytes, wrong declared type
        (b"GIF89a....", "image/png"),  # declared PNG, no PNG head
        (b"", "image/png"),
    ],
)
async def test_thumbnail_not_a_png_is_unexpected(respx_mock, content, content_type):
    respx_mock.get(f"{API}/library/files/5/plate-thumbnail/1").respond(
        200, content=content, headers={"content-type": content_type}
    )
    with pytest.raises(BambuddyUnexpected):
        await client().plate_thumbnail(5, 1)


async def test_thumbnail_without_content_type_but_png_head_is_taken(respx_mock):
    respx_mock.get(f"{API}/library/files/5/plate-thumbnail/1").respond(200, content=PNG)
    assert await client().plate_thumbnail(5, 1) == PNG


# ---- filament needs (Decision 8) ---------------------------------------------------


async def test_requirements(respx_mock):
    answer = load("requirements.json")
    route = respx_mock.get(f"{API}/library/files/5/filament-requirements", params={"plate_id": 1})
    route.respond(200, json=answer)
    needs = await client().filament_requirements(5, 1)
    assert route.call_count == 1
    assert len(needs) == len(answer["filaments"])
    for need, source in zip(needs, answer["filaments"], strict=True):
        assert need.slot == source["slot_id"]
        assert need.grams == source["used_grams"]
        assert need.material == source["type"]
        assert need.color_hex == source["color"].upper()  # fixture writes #RRGGBB (Task 1, guess 5)
    assert needs[0].color_hex == "#000000"
    assert needs[1].color_hex == "#FFFFFF"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("#ff0000", "#FF0000"),
        ("FF0000FF", "#FF0000"),
        ("ff0000", "#FF0000"),
        ("#ff000080", "#FF0000"),
        ("", None),
        (None, None),
        ("red", None),
        ("#FFF", None),
        ("#GG0000", None),
        ("#ff00", None),
        (123456, None),
    ],
)
def test_normalize_color(value, expected):
    assert normalize_color(value) == expected


def test_requirement_material_and_color_may_be_empty():
    needs = parse_requirements({"filaments": [{"slot_id": 1, "type": "", "color": "", "used_grams": 3}]})
    assert (needs[0].material, needs[0].color_hex, needs[0].grams) == (None, None, 3.0)
    assert isinstance(needs[0].grams, float)


# ---- unexpected answers (Review Focus 3) --------------------------------------------


def _file(**changes) -> dict:
    return {**load("files_root.json")[0], **changes}


@pytest.mark.parametrize(
    "parse, data",
    [
        (parse_folders, [{"name": "Templates", "parent_id": None, "children": []}]),  # no id
        (parse_folders, [{"id": "1", "name": "Templates", "children": []}]),
        (parse_folders, [{"id": 1, "name": "Templates", "children": "x"}]),
        (parse_folders, {"id": 1}),  # not a list
        (parse_files, [_file(id="5")]),  # id as text
        (parse_files, [{"id": 5}]),  # no filename
        (parse_files, [_file(filename=None)]),
        (parse_files, {"id": 5}),
        (parse_file, [_file()]),
        (parse_file, _file(file_hash=7)),
        (parse_plates, {"plates": "none"}),  # not a list
        (parse_plates, {"plates": {"index": 1}}),
        (parse_plates, {}),
        (parse_plates, [1]),
        (parse_plates, {"plates": [{"name": "Object 1"}]}),  # no index
        (parse_plates, {"plates": [{"index": "1"}]}),
        (parse_requirements, {"filaments": [{"type": "PLA", "color": "#000000", "used_grams": 1.0}]}),  # no slot_id
        (parse_requirements, {"filaments": [{"slot_id": 1, "used_grams": "42.5"}]}),  # grams as text
        (parse_requirements, {"filaments": [{"slot_id": 1}]}),  # no grams
        (parse_requirements, {"filaments": [{"slot_id": 1, "used_grams": True}]}),
        (parse_requirements, {"filaments": "x"}),
        (parse_requirements, []),
    ],
)
def test_library_unexpected(parse, data):
    with pytest.raises(BambuddyUnexpected):
        parse(data)


@pytest.mark.parametrize(
    "call, path, answer",
    [
        (lambda c: c.list_folders(), "/library/folders/", {"not": "a list"}),
        (lambda c: c.list_files(None), "/library/files/", [{"id": "5", "filename": "x"}]),
        (lambda c: c.get_file(5), "/library/files/5", [1, 2]),
        (lambda c: c.list_plates(5), "/library/files/5/plates", {"plates": 3}),
        (lambda c: c.filament_requirements(5, 1), "/library/files/5/filament-requirements", {"filaments": [{}]}),
    ],
)
async def test_client_methods_reject_wrong_shapes(respx_mock, call, path, answer):
    respx_mock.get(f"{API}{path}").respond(200, json=answer)
    with pytest.raises(BambuddyUnexpected):
        await call(client())
