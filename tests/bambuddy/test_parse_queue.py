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
    assert item.batch_id == request["batch_id"]
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
