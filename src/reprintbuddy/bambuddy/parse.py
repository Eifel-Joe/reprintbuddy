"""Turns Bambuddy's JSON into the data classes of `types.py`.

Pure functions without HTTP. Whatever does not match what Bambuddy's source sends
(wrong form, missing required field, wrong type) raises `BambuddyUnexpected`
instead of returning a half-filled object (Review Focus 3). Fields the parser does
not know are ignored, so a newer Bambuddy with more fields keeps working.
"""

from __future__ import annotations

import math
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
    Spool,
    SpoolMode,
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
    # Python's json reads the non-standard NaN/Infinity/-Infinity as floats; a number that
    # is not finite is a wrong answer, not a weight (it would give remaining 0.0 or inf).
    if isinstance(value, float) and not math.isfinite(value):
        return False
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
        try:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except OverflowError:  # e.g. 0001-01-01T00:00:00+01:00 lies before year 1 in UTC
            raise BambuddyUnexpected("a time is out of range") from None
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
        created_at=_utc(d.get("created_at")),
    )


def parse_queue_items(data: object) -> list[QueueItem]:
    """The queue list (`GET /queue/`), in the order of the answer (Bambuddy sorts by position)."""
    return [parse_queue_item(entry) for entry in _list(data, "queue list")]


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

    try:
        walk(data, None)
    except RecursionError:
        raise BambuddyUnexpected("folder tree is nested too deeply") from None
    return out


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_sliced(file_type: str | None, filename: str) -> bool:
    """Sliced for our purpose: Bambuddy's `file_type` is `gcode.3mf` (it judges by the zip content,
    `library.py:249-280`). Old entries (before Bambuddy #1600) may still say `3mf` for a
    `.gcode.3mf`, and a missing type follows the name. A raw `.gcode` cannot be queued for a
    Bambu printer in network mode (`library.py:338-368`), so it does not count."""
    if file_type == "gcode.3mf":
        return True
    return file_type in (None, "3mf") and filename.lower().endswith(".gcode.3mf")


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
        has_thumbnail=_has_text(d.get("thumbnail_path")),
        sliced=_is_sliced(_opt(d, "file_type", str), _req(d, "filename", str)),
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


def normalize_spool_color(value: object) -> str | None:
    """The colour of a spool: `#RRGGBB`, or `#RRGGBBAA` when the alpha part is not opaque.

    Unlike a print file's colour (`normalize_color`, alpha dropped, Decision 8) a spool keeps a
    transparency: alpha 00 means transparent and decides the filament sort (Spec 9.1). Bambuddy
    always sends eight digits and appends an opaque `FF` to a six-digit colour, so an opaque spool
    reads `#RRGGBB` as before.
    """
    text = normalize_color(value)
    if text is None:
        return None
    digits = value.strip().removeprefix("#")  # `normalize_color` has checked that it is a string
    if len(digits) == 8 and digits[6:].upper() != "FF":
        return text + digits[6:].upper()
    return text


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


def parse_spool_mode(data: object) -> SpoolMode:
    """The running inventory mode from `GET /spoolman/status` (Decision 17).

    Bambuddy resolves every spool route by `enabled` and a non-empty `url`
    (`api/routes/spoolman.py` `get_spoolman_status`); `connected` is a live check of
    Spoolman and does not decide the mode, so an unreachable Spoolman is still Spoolman mode.
    """
    d = _object(data, "Spoolman status")
    enabled = _req(d, "enabled", bool)
    url = _opt(d, "url", str)
    return "spoolman" if enabled and url else "builtin"


def parse_spools(data: object) -> list[Spool]:
    """The spools of either list (`GET /inventory/spools`, `GET /spoolman/inventory/spools`).

    Both routes send the same `InventorySpool` shape (Decision 12; Bambuddy
    `schemas/spool.py` `SpoolResponse`, `_spoolman_helpers.py` `_map_spoolman_spool`).
    """
    out: list[Spool] = []
    for entry in _list(data, "spool list"):
        d = _object(entry, "spool")
        label = _req(d, "label_weight", int)  # Bambuddy's schema types it as int
        used = _req(d, "weight_used", (int, float))
        # In Spoolman mode Bambuddy fills a missing colour name from the subtype and sets
        # `color_name_is_synthesized` (`_spoolman_helpers.py` `_map_spoolman_spool`). That is
        # no colour: B6 builds the variety from the subtype already. Only a real `true`
        # counts; missing, `false` or any other form means the name is as delivered.
        synthesized = d.get("color_name_is_synthesized") is True
        out.append(
            Spool(
                id=_req(d, "id", int),
                material=_req(d, "material", str),
                subtype=_opt(d, "subtype", str) or None,
                brand=_opt(d, "brand", str) or None,
                color_name=None if synthesized else (_opt(d, "color_name", str) or None),
                color_hex=normalize_spool_color(d.get("rgba")),
                label_g=label,
                remaining_g=max(0.0, float(label) - float(used)),
                archived=bool(d.get("archived_at")),
                created_at=_utc(d.get("created_at")),
            )
        )
    return out
