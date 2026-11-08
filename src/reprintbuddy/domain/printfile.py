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

# A 3MF holds a few dozen members. `zipfile` parses the whole central directory when it opens a
# file: it reads the directory's size from the end record and loops over that many bytes,
# whatever count the record claims. So the bound is on those bytes, read before `zipfile` opens
# the file and read the way `zipfile` reads them (see `_directory_bounds`). 1 MiB holds about
# 20,000 short entries; a real 3MF needs a few kilobytes.
MAX_ENTRIES = 10_000
MAX_DIRECTORY_BYTES = 1024 * 1024

_LOCAL_HEADER = b"PK\x03\x04"


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


def _directory_bounds(file: BinaryIO) -> tuple[int, int]:
    """(claimed entries, directory bytes) exactly as `zipfile` will see them; `not_zip` if unreadable.

    Root (security review v0.3.9): a reader of its own read the end record differently from
    `zipfile`, which always takes a zip64 locator right before the end record, even when the
    classic record looks honest, and which loops over the directory's bytes, not its count. A
    file could pass the check and still make `zipfile` parse millions of entries. Fix:
    `zipfile`'s own end-record reader (`_EndRecData`, in the standard library since Python 2,
    zip64 included), so both sides use the same numbers. Rejected: walking the directory by
    hand (a second parser to keep equal to `zipfile`). Tests: tests/domain/test_printfile.py.
    """
    try:
        record = zipfile._EndRecData(file)
    except (OSError, zipfile.BadZipFile, struct.error, ValueError, OverflowError):
        raise PrintFileError("not_zip") from None
    if not record:
        raise PrintFileError("not_zip")
    return record[zipfile._ECD_ENTRIES_TOTAL], record[zipfile._ECD_SIZE]


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
        entries, directory = _directory_bounds(file)
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
