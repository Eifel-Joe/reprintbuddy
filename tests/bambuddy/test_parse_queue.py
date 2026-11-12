"""Queue entries from Bambuddy's JSON (B2 Task 1, Decisions 12 and 13)."""

from datetime import datetime

import pytest
from fixtures import load

from reprintbuddy.bambuddy.client import BambuddyUnavailable, BambuddyUnexpected
from reprintbuddy.bambuddy.parse import _utc, parse_queue_item
from reprintbuddy.bambuddy.types import QUEUE_STATUSES, QueueItem


def test_queue_statuses_as_bambuddy_names_them():
    assert QUEUE_STATUSES == ("pending", "printing", "completed", "failed", "skipped", "cancelled")


def test_parse_pending_item():
    request = load("q4.request.json")
    item = parse_queue_item(load("q4.json"))
    assert isinstance(item, QueueItem)
    assert item.status == "pending"
    assert item.manual_start is False
    assert "batch_id" not in request and item.batch_id is None  # a single entry (Spec 7.6)
    assert item.created_at == datetime(2026, 11, 5, 9, 12, 44)  # `Z` -> naive UTC
    assert item.plate_id == request["plate_id"]
    assert item.library_file_id == request["library_file_id"]
    assert item.target_model == request["target_model"]
    assert item.created_by == "reprintbuddy"
    assert item.label == f"Template A.gcode.3mf / plate {request['plate_id']}"
    assert item.printer_id is None and item.printer_name is None
    assert isinstance(item.position, int)
    assert item.started_at is None and item.completed_at is None
    assert item.waiting_reason is None and item.error_message is None


def test_parse_printing_and_completed():
    printing = parse_queue_item(load("q4_printing.json"))
    assert printing.status == "printing"
    assert isinstance(printing.printer_id, int)
    assert printing.printer_name == "Printer 1"
    assert isinstance(printing.started_at, datetime)
    assert printing.started_at.tzinfo is None
    assert printing.completed_at is None

    completed = parse_queue_item(load("q4_completed.json"))
    assert completed.status == "completed"
    assert isinstance(completed.completed_at, datetime)
    assert completed.completed_at.tzinfo is None
    assert completed.completed_at > completed.started_at


def test_every_recorded_entry_parses():
    for name in ("queue_open.json", "queue_all.json"):
        items = [parse_queue_item(entry) for entry in load(name)]
        assert items and all(item.status in QUEUE_STATUSES for item in items)


def test_label_fallbacks():
    q4 = load("q4.json")
    archive_only = {**q4, "library_file_name": None, "archive_name": "Template C"}
    assert parse_queue_item(archive_only).label == f"Template C / plate {q4['plate_id']}"
    neither = {**q4, "library_file_name": None, "archive_name": None}
    assert parse_queue_item(neither).label == f"#{q4['id']} / plate {q4['plate_id']}"
    no_plate = {**q4, "plate_id": None}
    assert parse_queue_item(no_plate).label == "Template A.gcode.3mf"
    missing_keys = {k: v for k, v in q4.items() if k not in ("library_file_name", "archive_name", "plate_id")}
    assert parse_queue_item(missing_keys).label == f"#{q4['id']}"


def _q4(**changes):
    return {**load("q4.json"), **changes}


@pytest.mark.parametrize(
    "body",
    [
        [],
        "text",
        {},
        None,
        _q4(id="7"),
        _q4(id=True),
        _q4(status=None),
        _q4(status=""),
        _q4(status=3),
        _q4(manual_start="no"),
        _q4(manual_start=None),
        _q4(started_at="gestern"),
        _q4(started_at=1730800000),
        _q4(created_at="gestern"),
        _q4(created_at=1730800000),
        _q4(batch_id="12"),
        _q4(plate_id=False),
        _q4(created_by_username=7),
    ],
    ids=lambda body: repr(body)[:40],
)
def test_unexpected_bodies(body):
    with pytest.raises(BambuddyUnexpected):
        parse_queue_item(body)


def test_unexpected_counts_as_unavailable():
    """Every caller treats an unexpected answer like an unclear outcome (Decision 2)."""
    with pytest.raises(BambuddyUnavailable):
        parse_queue_item([])


def test_unknown_status_passed_raw():
    """Decision 13: B4 decides what a new status means."""
    assert parse_queue_item(_q4(status="paused")).status == "paused"


def test_unknown_fields_ignored():
    item = parse_queue_item(_q4(neu=1))
    assert item.status == "pending"


@pytest.mark.parametrize(
    "text",
    ["2026-11-05T10:00:00Z", "2026-11-05T11:00:00+01:00", "2026-11-05T10:00:00"],
)
def test_utc_formats(text):
    value = _utc(text)
    assert value == datetime(2026, 11, 5, 10, 0)
    assert value.tzinfo is None


@pytest.mark.parametrize("text", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-05:00"])
def test_utc_out_of_range_is_unexpected(text):
    with pytest.raises(BambuddyUnexpected):
        _utc(text)


def test_utc_keeps_fractions_and_none():
    assert _utc("2026-11-05T09:13:52.640917Z") == datetime(2026, 11, 5, 9, 13, 52, 640917)
    assert _utc(None) is None


@pytest.mark.parametrize("value", ["gestern", "", 1730800000, True, [], {}])
def test_utc_rejects_other_values(value):
    with pytest.raises(BambuddyUnexpected):
        _utc(value)


def test_created_at_missing_or_null_is_none():
    body = load("q4.json")
    del body["created_at"]
    assert parse_queue_item(body).created_at is None
    assert parse_queue_item(dict(load("q4.json"), created_at=None)).created_at is None


def test_created_at_with_offset_becomes_utc():
    item = parse_queue_item(dict(load("q4.json"), created_at="2026-11-05T10:12:44+01:00"))
    assert item.created_at == datetime(2026, 11, 5, 9, 12, 44)


def test_queue_item_reads_print_time_location_schedule():
    """What the print plan needs of an entry (Spec 10.2, 0.4.2): the slicer's print time in seconds,
    the place, a later start and whether Bambuddy already let it be jumped (`schemas/print_queue.py:214-344`)."""
    item = parse_queue_item(_q4(
        print_time_seconds=5400, target_location="Shelf B", scheduled_time="2026-11-12T15:00:00Z",
        been_jumped=True,
    ))
    assert item.print_time_seconds == 5400
    assert item.target_location == "Shelf B"
    assert item.scheduled_time == datetime(2026, 11, 12, 15, 0)
    assert item.been_jumped is True
    assert item.file_name == "Template A.gcode.3mf"  # without " / plate n"
    archive = parse_queue_item(_q4(library_file_name=None, archive_name="Template C"))
    assert archive.file_name == "Template C"
    bare = parse_queue_item({k: v for k, v in load("q4.json").items()
                             if k not in ("print_time_seconds", "target_location", "scheduled_time", "been_jumped")})
    assert bare.print_time_seconds is None and bare.target_location is None
    assert bare.scheduled_time is None and bare.been_jumped is False


@pytest.mark.parametrize(("field", "value"), [
    ("print_time_seconds", "long"), ("print_time_seconds", -5), ("print_time_seconds", True),
    ("target_location", 7), ("scheduled_time", "soon"), ("been_jumped", "yes"),
])
def test_plan_fields_of_an_odd_form_do_not_break_the_entry(field, value):
    """The plan's fields are only shown; the same entry is read to hand over and to follow prints
    (Spec 7.3, 7.6), so an odd value is shown as unknown instead of refusing the answer."""
    item = parse_queue_item(_q4(**{field: value}))
    assert item.status == "pending"
    assert getattr(item, field) in (None, False)


def test_print_time_as_a_float_is_whole_seconds():
    assert parse_queue_item(_q4(print_time_seconds=3720.6)).print_time_seconds == 3720
