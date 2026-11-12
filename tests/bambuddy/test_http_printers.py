"""Reading printers through `HttpBambuddyClient` (B2 Task 4a, amended Decision 9)."""

import dataclasses

import pytest
from fixtures import API, client, load

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable, BambuddyUnexpected
from reprintbuddy.bambuddy.types import Printer


async def test_list_printers(respx_mock):
    route = respx_mock.get(f"{API}/printers/").respond(200, json=load("printers.json"))
    printers = await client().list_printers()
    assert route.called
    assert printers == [
        Printer(1, "Printer 1", "A1", True),
        Printer(2, "Printer 2", "A1", True),
        Printer(3, "Printer 3", "P1S", True),
    ]


async def test_list_printers_leaves_out_printers_without_model(respx_mock):
    data = load("printers.json")
    no_model = dict(data[0], id=7, name="No model")
    del no_model["model"]
    empty_model = dict(data[0], id=8, name="Empty model", model="")
    null_model = dict(data[0], id=9, name="Null model", model=None)
    respx_mock.get(f"{API}/printers/").respond(200, json=data + [no_model, empty_model, null_model])
    assert [p.id for p in await client().list_printers()] == [1, 2, 3]


async def test_list_printers_keeps_maintenance_printer_inactive(respx_mock):
    respx_mock.get(f"{API}/printers/").respond(200, json=load("printers_maintenance.json"))
    printers = {p.id: p for p in await client().list_printers()}
    assert printers[2] == Printer(2, "Printer 2", "A1", False)
    assert printers[2].active is False
    assert printers[1].active is True and printers[3].active is True


async def test_printer_answer_has_no_secret(respx_mock):
    respx_mock.get(f"{API}/printers/").respond(200, json=load("printers.json"))
    printers = await client().list_printers()
    assert printers
    for printer in printers:
        assert [f.name for f in dataclasses.fields(printer)] == ["id", "name", "model", "active", "location"]
        text = repr(printer)
        assert "203.0.113" not in text and "SERIAL" not in text


def test_printer_active_defaults_to_true():
    assert Printer(1, "P", "A1").active is True


def _no_id():
    d = load("printers.json")
    del d[0]["id"]
    return d


def _text_id():
    d = load("printers.json")
    d[0]["id"] = "1"
    return d


def _text_active():
    d = load("printers.json")
    d[0]["is_active"] = "false"
    return d


def _no_active():
    d = load("printers.json")
    del d[0]["is_active"]
    return d


def _null_active():
    d = load("printers.json")
    d[0]["is_active"] = None
    return d


@pytest.mark.parametrize(
    "answer",
    [{}, _no_id(), _text_id(), _text_active(), _no_active(), _null_active()],
    ids=["not-a-list", "no-id", "text-id", "text-is-active", "no-is-active", "null-is-active"],
)
async def test_printers_unexpected(respx_mock, answer):
    respx_mock.get(f"{API}/printers/").respond(200, json=answer)
    with pytest.raises(BambuddyUnexpected):
        await client().list_printers()


async def test_printers_rejected(respx_mock):
    respx_mock.get(f"{API}/printers/").respond(403, json={"detail": "no"})
    with pytest.raises(BambuddyRejected) as rejected:
        await client().list_printers()
    assert rejected.value.status == 403


async def test_printers_server_error(respx_mock):
    respx_mock.get(f"{API}/printers/").respond(500)
    with pytest.raises(BambuddyUnavailable):
        await client().list_printers()


async def test_printer_location_is_read(respx_mock):
    """The place of a printer (Bambuddy `schemas/printer.py` `location`): an entry with a place goes
    only to printers there (`print_scheduler.py:3310`), so the print plan needs it (0.4.2)."""
    data = load("printers.json")
    data[1]["location"] = "Shelf B"
    respx_mock.get(f"{API}/printers/").respond(200, json=data)
    printers = {p.id: p for p in await client().list_printers()}
    assert printers[1].location is None
    assert printers[2].location == "Shelf B"
