"""Turns Bambuddy's JSON into the data classes of `types.py`.

Pure functions without HTTP. Whatever does not match what Bambuddy's source sends
(wrong form, missing required field, wrong type) raises `BambuddyUnexpected`
instead of returning a half-filled object (Review Focus 3). Fields the parser does
not know are ignored, so a newer Bambuddy with more fields keeps working.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from reprintbuddy.bambuddy.client import BambuddyUnexpected
from reprintbuddy.bambuddy.types import (
    FilamentRequirement,
    LibraryFile,
    LibraryFolder,
    Plate,
    Printer,
    QueueItem,
)

_Types = type | tuple[type, ...]


def _matches(value: object, types: _Types) -> bool:
    wanted = types if isinstance(types, tuple) else (types,)
    # `True` is an `int` to Python, never to Bambuddy's JSON: an id or count that comes
    # as a truth value is a wrong answer.
    if isinstance(value, bool) and bool not in wanted:
        return False
    if float in wanted and isinstance(value, int):
        return True  # JSON writes 42.0 as 42
    return isinstance(value, wanted)


def _cast(value: Any, types: _Types) -> Any:
    wanted = types if isinstance(types, tuple) else (types,)
    if float in wanted and int not in wanted and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _req(d: dict, key: str, types: _Types) -> Any:
    """A field the source always fills: missing, `null` or of another type is unexpected."""
    if key not in d:
        raise BambuddyUnexpected(f"field {key!r} is missing")
    value = d[key]
    if not _matches(value, types):
        raise BambuddyUnexpected(f"field {key!r} has an unexpected value")
    return _cast(value, types)


def _opt(d: dict, key: str, types: _Types) -> Any:
    """A field that may be missing or `null` (→ None); any other type is unexpected."""
    value = d.get(key)
    if value is None:
        return None
    if not _matches(value, types):
        raise BambuddyUnexpected(f"field {key!r} has an unexpected value")
    return _cast(value, types)


def _utc(value: object) -> datetime | None:
    """A time from Bambuddy as naive UTC (Decision 12).

    Bambuddy writes queue times with `Z` (`schemas/print_queue.py` `serialize_utc_datetime`),
    library and spool times without a zone, and Spoolman's own texts as they come. With a
    zone or offset the time is converted to UTC; without one it already is UTC (Bambuddy
    stores naive UTC). `None` stays `None`; anything else is unexpected.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise BambuddyUnexpected("a time is not a text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise BambuddyUnexpected("a time is not in ISO format") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _object(data: object, what: str) -> dict:
    if not isinstance(data, dict):
        raise BambuddyUnexpected(f"{what} is not an object")
    return data


def _label(item_id: int, file_name: str | None, archive_name: str | None, plate_id: int | None) -> str:
    label = file_name or archive_name or f"#{item_id}"
    if plate_id is not None:
        label += f" / plate {plate_id}"
    return label


def parse_queue_item(data: object) -> QueueItem:
    """One entry of `PrintQueueItemResponse` (Bambuddy `schemas/print_queue.py:215-325`)."""
    d = _object(data, "queue entry")
    item_id = _req(d, "id", int)
    status = _req(d, "status", str)
    if not status.strip():
        raise BambuddyUnexpected("field 'status' is empty")
    plate_id = _opt(d, "plate_id", int)
    return QueueItem(
        id=item_id,
        status=status,
        batch_id=_opt(d, "batch_id", int),
        position=_opt(d, "position", int),
        printer_id=_opt(d, "printer_id", int),
        target_model=_opt(d, "target_model", str),
        label=_label(item_id, _opt(d, "library_file_name", str), _opt(d, "archive_name", str), plate_id),
        created_by=_opt(d, "created_by_username", str),
        library_file_id=_opt(d, "library_file_id", int),
        plate_id=plate_id,
        printer_name=_opt(d, "printer_name", str),
        manual_start=_req(d, "manual_start", bool),
        waiting_reason=_opt(d, "waiting_reason", str),
        started_at=_utc(d.get("started_at")),
        completed_at=_utc(d.get("completed_at")),
        error_message=_opt(d, "error_message", str),
    )


# ---- library ---------------------------------------------------------------------------


def _list(data: object, what: str) -> list:
    if not isinstance(data, list):
        raise BambuddyUnexpected(f"{what} is not a list")
    return data


def parse_folders(data: object) -> list[LibraryFolder]:
    """The folder tree (`LibraryFolderTreeResponse`, Bambuddy `library.py:1135-1158`) as a flat list.

    Pre-order, in the order of the answer (Decision 6). `parent_id` is the field of the
    entry; a child that lacks it gets the id of the folder it sits in.
    """
    out: list[LibraryFolder] = []

    def walk(level: object, enclosing: int | None) -> None:
        for entry in _list(level, "folder list"):
            d = _object(entry, "folder")
            folder_id = _req(d, "id", int)
            parent = _opt(d, "parent_id", int)
            out.append(
                LibraryFolder(
                    id=folder_id,
                    name=_req(d, "name", str),
                    parent_id=enclosing if parent is None else parent,
                )
            )
            children = d.get("children")
            if children is not None:
                walk(children, folder_id)

    walk(data, None)
    return out


def parse_file(data: object) -> LibraryFile:
    """One library file (`FileListResponse` / `FileResponse`, Bambuddy `schemas/library.py`).

    `fingerprint` is `file_hash` (Decision 7): the list has none, an external file has none.
    """
    d = _object(data, "library file")
    return LibraryFile(
        id=_req(d, "id", int),
        filename=_req(d, "filename", str),
        folder_id=_opt(d, "folder_id", int),
        fingerprint=_opt(d, "file_hash", str) or None,
        printer_model=_opt(d, "sliced_for_model", str) or None,
    )


def parse_files(data: object) -> list[LibraryFile]:
    # List entries never carry a fingerprint (Decision 7), whatever the entry holds.
    return [replace(parse_file(entry), fingerprint=None) for entry in _list(data, "file list")]


def parse_plates(data: object) -> list[Plate]:
    """The plates of a file, from `data["plates"]` (Bambuddy `library.py:3761-3787`)."""
    plates = _list(_object(data, "plate answer").get("plates"), "field 'plates'")
    out: list[Plate] = []
    for entry in plates:
        d = _object(entry, "plate")
        out.append(Plate(index=_req(d, "index", int), name=_opt(d, "name", str)))
    return out


_HEX = frozenset("0123456789abcdefABCDEF")


def normalize_color(value: object) -> str | None:
    """`#RRGGBB` in capitals from `#RRGGBB`, `RRGGBB`, `#RRGGBBAA` or `RRGGBBAA`; else `None`.

    The alpha part is dropped (Decision 8).
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) not in (6, 8) or not set(text) <= _HEX:
        return None
    return "#" + text[:6].upper()


def parse_requirements(data: object) -> list[FilamentRequirement]:
    """The filament needs of one plate, from `data["filaments"]` (Bambuddy `library.py:3958-3973`)."""
    entries = _list(_object(data, "requirements answer").get("filaments"), "field 'filaments'")
    out: list[FilamentRequirement] = []
    for entry in entries:
        d = _object(entry, "filament")
        out.append(
            FilamentRequirement(
                slot=_req(d, "slot_id", int),
                grams=_req(d, "used_grams", float),
                material=_opt(d, "type", str) or None,
                color_hex=normalize_color(d.get("color")),
            )
        )
    return out


def parse_printers(data: object) -> list[Printer]:
    """The printer list (`PrinterResponse`, Bambuddy `printers.py` `GET /printers/`).

    Only id, name, model and `is_active` are kept; address and serial number are not read.
    `is_active` is required (the source has no default for it). A printer without a model is left out: no "Any <model>" target exists for it. A printer
    in maintenance (`is_active: false`) is kept with `active=False`.
    """
    out: list[Printer] = []
    for entry in _list(data, "printer list"):
        d = _object(entry, "printer")
        printer_id = _req(d, "id", int)
        model = _opt(d, "model", str)
        name = _req(d, "name", str)
        active = _req(d, "is_active", bool)
        if not model:
            continue
        out.append(Printer(printer_id, name, model, active))
    return out
