"""Checks for a print file uploaded through Reprintbuddy (Spec 5.1a, v0.3.9 p20).

Reprintbuddy cannot delete a file once it is in Bambuddy, so everything is checked before the
file is sent. The functions here are pure: no request, no Bambuddy.
"""

from __future__ import annotations

import struct
import unicodedata
import zipfile
from collections.abc import Iterable
from typing import BinaryIO

from reprintbuddy.bambuddy.types import LibraryFolder


def is_external_folder(folders: Iterable[LibraryFolder], folder_id: int | None) -> bool:
    """True when the folder or any folder above it is external (`is_external`).

    Bambuddy marks the sub-folders its scan creates as external too; walking up also covers
    an internal folder hung below an external one. The top level is never external. An
    unknown id is not external here; that it must exist is checked where it is used.
    """
    by_id = {folder.id: folder for folder in folders}
    seen: set[int] = set()
    current = by_id.get(folder_id) if folder_id is not None else None
    while current is not None and current.id not in seen:
        if current.is_external:
            return True
        seen.add(current.id)
        current = by_id.get(current.parent_id) if current.parent_id is not None else None
    return False


# ---- checks of the file itself (Spec 5.1a) ---------------------------------------------------


MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # fixed, no setting (Spec 5.1a)

# Bambuddy's name rules (`utils/filename.py` `validate_print_filename`, tag
# v1.3.1b1-daily.20261107): the SD card's FAT32/exFAT set, no control character below 0x20, no
# trailing space or dot, at most 255 bytes UTF-8.
_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
MAX_NAME_BYTES = 255

# A 3MF holds a few dozen members. The bounds are read from the zip's end record before
# `zipfile` parses the directory, which it does in full when it opens the file: millions of
# claimed entries in a 200 MB file would cost gigabytes before anything could count them.
MAX_ENTRIES = 10_000
MAX_DIRECTORY_BYTES = 16 * 1024 * 1024

_LOCAL_HEADER = b"PK\x03\x04"
_END = b"PK\x05\x06"
_END_SIZE = 22
_ZIP64_LOCATOR = 0x07064B50
_ZIP64_END = 0x06064B50


class PrintFileError(ValueError):
    """A file that is not sent; `code` names why (a message key `upload.error.<code>`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def name_key(name: str) -> str:
    """The form two names are compared in: composed (NFC) and case-folded (Spec 5.1a)."""
    return unicodedata.normalize("NFC", name).casefold()


def upload_name(raw: str | None) -> str:
    """The name the file is sent under, composed (NFC), or `PrintFileError`.

    A browser sends only the file's name. A name with `/` or `\\` is refused, not cut to its
    last part: it is used only as a name, never as a path (Spec 5.1a).
    """
    if raw is None or not raw.strip():
        raise PrintFileError("name_empty")
    name = unicodedata.normalize("NFC", raw)
    if name in (".", "..") or any(c in _FORBIDDEN_CHARS or ord(c) < 0x20 for c in name):
        raise PrintFileError("name_invalid")
    if name.endswith((" ", ".")):
        raise PrintFileError("name_invalid")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise PrintFileError("name_too_long")
    if not name.casefold().endswith(".3mf"):
        raise PrintFileError("not_3mf")
    return name


def _directory_bounds(file: BinaryIO, size: int) -> tuple[int, int]:
    """(entries, directory bytes) from the end record, zip64 included; `not_zip` if unreadable."""
    tail_length = min(size, _END_SIZE + 0xFFFF)
    file.seek(size - tail_length)
    tail = file.read(tail_length)
    at = tail.rfind(_END)
    if at < 0 or len(tail) - at < _END_SIZE:
        raise PrintFileError("not_zip")
    _sig, _disk, _cd_disk, on_disk, entries, directory, offset, _comment = struct.unpack_from("<IHHHHIIH", tail, at)
    if 0xFFFF in (on_disk, entries) or 0xFFFFFFFF in (directory, offset):
        locator_at = size - tail_length + at - 20
        if locator_at < 0:
            raise PrintFileError("not_zip")
        file.seek(locator_at)
        signature, _disk, record_at, _disks = struct.unpack("<IIQI", file.read(20))
        if signature != _ZIP64_LOCATOR or record_at > size - 56:
            raise PrintFileError("not_zip")
        file.seek(record_at)
        record = struct.unpack("<IQHHIIQQQQ", file.read(56))
        if record[0] != _ZIP64_END:
            raise PrintFileError("not_zip")
        entries, directory = record[7], record[8]
    return entries, directory


def check_sliced_3mf(file: BinaryIO, *, limit: int = MAX_UPLOAD_BYTES) -> None:
    """A zip of at most `limit` bytes that carries G-code, or `PrintFileError`; rewound after.

    Sliced as Bambuddy judges it (`threemf_tools.py` `names_carry_gcode`): a member whose name
    ends in `.gcode`, case as written. Only the directory is read; nothing is unpacked.
    """
    try:
        size = file.seek(0, 2)
        if size > limit:
            raise PrintFileError("too_large")
        file.seek(0)
        if file.read(4) != _LOCAL_HEADER:
            raise PrintFileError("not_zip")
        entries, directory = _directory_bounds(file, size)
        if entries > MAX_ENTRIES or directory > MAX_DIRECTORY_BYTES:
            raise PrintFileError("not_zip")
        file.seek(0)
        try:
            with zipfile.ZipFile(file) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, OSError, ValueError, EOFError, struct.error, NotImplementedError, RuntimeError):
            raise PrintFileError("not_zip") from None
        if not any(name.endswith(".gcode") for name in names):
            raise PrintFileError("not_sliced")
    except struct.error:
        raise PrintFileError("not_zip") from None
    finally:
        file.seek(0)
