"""The state of the printers for the print plan (Spec 7.6 "Druckerzustand für den Druckplan", 10.2).

One request for all printers (`GET /bulk/printer-statuses`, Bambuddy 1.3.0 and later, the version Reprintbuddy
needs). Any refusal, a 404 included, leaves the state unknown; the engine takes care of that.
"""

import httpx
import pytest
from fixtures import API, client, load

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable, BambuddyUnexpected
from reprintbuddy.bambuddy.parse import parse_printer_status
from reprintbuddy.bambuddy.types import PrinterStatus


def _row(printer_id: int, **changes) -> dict:
    data = load("printer_status.json")
    data.update(id=printer_id, name=f"Printer {printer_id}", **changes)
    return data


async def test_bulk_asks_once_for_all_printers(respx_mock):
    route = respx_mock.get(f"{API}/bulk/printer-statuses").respond(
        200, json=[_row(1), _row(2, state="IDLE", progress=0.0, remaining_time=0)]
    )
    found = await client().printer_statuses([1, 2])
    assert route.call_count == 1
    assert route.calls.last.request.url.params["ids"] == "1,2"
    assert set(found) == {1, 2}
    first = found[1]
    assert first == PrinterStatus(
        printer_id=1, connected=True, state="RUNNING", progress=63.0, remaining_min=70,
        subtask_name="Bracket small", awaiting_plate_clear=False, stopped_error=False,
    )
    assert found[2].state == "IDLE"


async def test_bulk_404_is_a_refusal_without_single_requests(respx_mock):
    """Reprintbuddy needs Bambuddy 1.3 or newer, which has the one request for all printers. A Bambuddy that
    answers 404 is refused like any other (the print plan says "state unknown"); nothing is asked printer by
    printer (decided 12.11.2026)."""
    respx_mock.get(f"{API}/bulk/printer-statuses").respond(404, json={"detail": "Not Found"})
    single = respx_mock.get(url__regex=rf"{API}/printers/\d+/status").respond(200, json=_row(1))
    with pytest.raises(BambuddyRejected) as caught:
        await client().printer_statuses([1, 2])
    assert caught.value.status == 404
    assert not single.called


@pytest.mark.parametrize("status", [401, 403])
async def test_refused_status_raises_rejected(respx_mock, status):
    respx_mock.get(f"{API}/bulk/printer-statuses").respond(status, json={"detail": "no"})
    with pytest.raises(BambuddyRejected):
        await client().printer_statuses([1])


async def test_unreachable_raises_unavailable(respx_mock):
    respx_mock.get(f"{API}/bulk/printer-statuses").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(BambuddyUnavailable):
        await client().printer_statuses([1])


async def test_no_ids_no_request(respx_mock):
    route = respx_mock.get(f"{API}/bulk/printer-statuses").respond(200, json=[])
    assert await client().printer_statuses([]) == {}
    assert not route.called


def test_offline_values_are_dropped():
    """Bambuddy keeps the last state, progress and remaining time of a printer that went silent
    (`bambu_mqtt.py:1878`); they do not hold any more (Spec 7.6)."""
    status = parse_printer_status(_row(1, connected=False, state="RUNNING", progress=40.0, remaining_time=30))
    assert status.connected is False
    assert status.state is None and status.progress is None and status.remaining_min is None


@pytest.mark.parametrize(("errors", "stopped"), [
    ([], False),
    ([{"code": "0300", "module": 3, "severity": 2}], False),
    ([{"code": "0300", "module": 3, "severity": 3}, {"code": "0500", "module": 5, "severity": 1}], True),
])
def test_stopped_error_from_hms(errors, stopped):
    assert parse_printer_status(_row(1, hms_errors=errors)).stopped_error is stopped


def test_id_from_the_live_message():
    """A live message carries the printer's id beside its data (`printer_status`, `main.py:2425 ff.`)."""
    data = _row(1)
    del data["id"]
    assert parse_printer_status(data, 7).printer_id == 7


def test_missing_fields_are_unknown():
    status = parse_printer_status({"id": 3, "connected": True})
    assert status == PrinterStatus(3, True, None, None, None, None, False, False)


@pytest.mark.parametrize("data", [[], {"id": 1}, {"connected": True}, {"id": "1", "connected": True}])
def test_wrong_shape_is_unexpected(data):
    with pytest.raises(BambuddyUnexpected):
        parse_printer_status(data)


async def test_shortest_first_from_the_settings(respx_mock):
    respx_mock.get(f"{API}/settings/").respond(200, json={"queue_shortest_first": True, "default_timelapse": False})
    assert await client().shortest_first() is True


async def test_shortest_first_missing_is_off(respx_mock):
    respx_mock.get(f"{API}/settings/").respond(200, json={"default_timelapse": False})
    assert await client().shortest_first() is False


@pytest.mark.parametrize("minutes", [-5, 7 * 24 * 60 + 1, 4294967295])
def test_impossible_remaining_time_is_unknown(minutes):
    """A remaining time below 0 or beyond 7 days is no estimate (a sensor glitch, `uint32` max): unknown,
    so the print plan neither draws into the past nor fails on a date out of range (review v0.4.2, 2)."""
    assert parse_printer_status(_row(1, remaining_time=minutes)).remaining_min is None


def test_progress_is_kept_between_0_and_100():
    assert parse_printer_status(_row(1, progress=140.0)).progress == 100.0
    assert parse_printer_status(_row(1, progress=-3.0)).progress == 0.0
