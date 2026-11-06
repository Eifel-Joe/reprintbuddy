"""Turns Bambuddy's JSON into the data classes of `types.py`.

Pure functions without HTTP. Whatever does not match what Bambuddy's source sends
(wrong form, missing required field, wrong type) raises `BambuddyUnexpected`
instead of returning a half-filled object (Review Focus 3). Fields the parser does
not know are ignored, so a newer Bambuddy with more fields keeps working.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from reprintbuddy.bambuddy.client import BambuddyUnexpected
from reprintbuddy.bambuddy.types import QueueItem

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
