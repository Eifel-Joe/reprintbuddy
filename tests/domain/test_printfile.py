"""Checks of an uploaded print file before anything is sent (Spec 5.1a, v0.3.9 p20).

Name rules as Bambuddy's `utils/filename.py` `validate_print_filename`; sliced as Bambuddy's
`threemf_tools.py` `names_carry_gcode` (a member ending in `.gcode`, case as written).
"""

import io
import struct
import unicodedata
import zipfile

import pytest

from reprintbuddy.domain.printfile import (
    MAX_UPLOAD_BYTES,
    PrintFileError,
    check_sliced_3mf,
    name_key,
    upload_name,
)


def zip_bytes(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"data")
    return buffer.getvalue()


SLICED = zip_bytes("3D/3dmodel.model", "Metadata/plate_1.gcode", "Metadata/plate_1.png")


def code_of(call, *args, **kwargs) -> str:
    with pytest.raises(PrintFileError) as caught:
        call(*args, **kwargs)
    return caught.value.code


# ---- the name ---------------------------------------------------------------------------------


def test_the_limit_is_200_mb():
    assert MAX_UPLOAD_BYTES == 200 * 1024 * 1024


@pytest.mark.parametrize("name", ["Template A.gcode.3mf", "a.3mf", "B.3MF", "x.GCODE.3mf", " leading.3mf", "Größe.3mf"])
def test_good_names(name):
    assert upload_name(name) == unicodedata.normalize("NFC", name)


def test_a_decomposed_name_is_sent_composed():
    decomposed = unicodedata.normalize("NFD", "Größe.3mf")
    assert decomposed != "Größe.3mf"
    assert upload_name(decomposed) == "Größe.3mf"


@pytest.mark.parametrize("name", [None, "", "   "])
def test_empty_name(name):
    assert code_of(upload_name, name) == "name_empty"


@pytest.mark.parametrize("name", ["dir/a.3mf", "C:\\Users\\a.3mf", "../a.3mf"])
def test_a_path_is_refused_not_cut(name):
    assert code_of(upload_name, name) == "name_invalid"


@pytest.mark.parametrize("char", list('<>:"|?*') + ["\x00", "\x1f", "\n", "\t"])
def test_each_character_bambuddy_refuses(char):
    assert code_of(upload_name, f"a{char}b.3mf") == "name_invalid"


@pytest.mark.parametrize("name", [".", "..", "a.3mf.", "a.3mf "])
def test_dots_and_trailing_space(name):
    assert code_of(upload_name, name) == "name_invalid"


def test_255_bytes_pass_256_do_not():
    stem = "ä" * 125  # 250 bytes
    assert upload_name(stem + "x.3mf") == stem + "x.3mf"  # 255 bytes
    assert code_of(upload_name, stem + "xy.3mf") == "name_too_long"  # 256 bytes


@pytest.mark.parametrize("name", ["a.stl", "a.gcode", "a.3mf.zip", "a", "a3mf"])
def test_only_3mf(name):
    assert code_of(upload_name, name) == "not_3mf"


def test_name_key_ignores_case_and_composition():
    assert name_key("GRÖSSE.3MF") == name_key(unicodedata.normalize("NFD", "größe.3mf"))
    assert name_key("a.3mf") != name_key("b.3mf")


# ---- the content -----------------------------------------------------------------------------


def test_a_sliced_3mf_passes_and_is_rewound():
    content = io.BytesIO(SLICED)
    content.seek(7)
    check_sliced_3mf(content)
    assert content.tell() == 0


def test_without_gcode_is_not_sliced():
    assert code_of(check_sliced_3mf, io.BytesIO(zip_bytes("3D/3dmodel.model"))) == "not_sliced"


def test_gcode_is_found_case_as_written_like_bambuddy():
    assert code_of(check_sliced_3mf, io.BytesIO(zip_bytes("Metadata/plate_1.GCODE"))) == "not_sliced"


def test_gcode_anywhere_counts_like_bambuddy():
    check_sliced_3mf(io.BytesIO(zip_bytes("other/output.gcode")))


@pytest.mark.parametrize(
    "data",
    [b"", b"not a zip at all", b"PK\x03\x04" + b"\x00" * 40, SLICED[:-30], b"PK\x05\x06" + b"\x00" * 18],
)
def test_not_a_readable_zip(data):
    assert code_of(check_sliced_3mf, io.BytesIO(data)) == "not_zip"


def test_too_large_is_refused_before_it_is_opened():
    assert code_of(check_sliced_3mf, io.BytesIO(SLICED), limit=len(SLICED) - 1) == "too_large"
    check_sliced_3mf(io.BytesIO(SLICED), limit=len(SLICED))


def _with_end_record(data: bytes, entries: int, cd_size: int) -> bytes:
    """`data` with its end record claiming other numbers."""
    end = data.rindex(b"PK\x05\x06")
    record = bytearray(data[end:])
    struct.pack_into("<HHI", record, 8, min(entries, 0xFFFF), min(entries, 0xFFFF), cd_size)
    return data[:end] + bytes(record)


def test_an_end_record_with_too_many_entries_is_refused_before_parsing(monkeypatch):
    opened = []
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: opened.append(a))
    assert code_of(check_sliced_3mf, io.BytesIO(_with_end_record(SLICED, 0xFFFE, 100))) == "not_zip"
    assert opened == []


def test_an_end_record_with_a_huge_directory_is_refused_before_parsing(monkeypatch):
    opened = []
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: opened.append(a))
    assert code_of(check_sliced_3mf, io.BytesIO(_with_end_record(SLICED, 3, 64 * 1024 * 1024))) == "not_zip"
    assert opened == []


def test_a_zip64_end_record_is_read(monkeypatch):
    """A zip64 file names its true counts in the zip64 record; those are the ones checked."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", allowZip64=True) as archive:
        archive.writestr("Metadata/plate_1.gcode", b"g")
    data = buffer.getvalue()
    end = data.rindex(b"PK\x05\x06")
    # Mark the classic record as "see zip64" and put a zip64 locator and record in front of it.
    cd_size, cd_offset = struct.unpack_from("<II", data, end + 12)
    zip64_record = struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, 200_000, 200_000, cd_size, cd_offset)
    locator = struct.pack("<IIQI", 0x07064B50, 0, end, 1)
    classic = bytearray(data[end:])
    struct.pack_into("<HHII", classic, 8, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    forged = data[:end] + zip64_record + locator + bytes(classic)
    opened = []
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: opened.append(a))
    assert code_of(check_sliced_3mf, io.BytesIO(forged)) == "not_zip"
    assert opened == []
