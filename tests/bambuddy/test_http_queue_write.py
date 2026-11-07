"""Creating queue entries in Bambuddy (B2 Task 6, Decision 11; single entries since v0.3.5)."""

import dataclasses
import json

import httpx
import pytest
from fixtures import API, client, load

from reprintbuddy.bambuddy import client as client_module
from reprintbuddy.bambuddy import parse
from reprintbuddy.bambuddy.client import (
    BambuddyClient,
    BambuddyRejected,
    BambuddyUnavailable,
    BambuddyUnexpected,
    HttpBambuddyClient,
)
from reprintbuddy.bambuddy.types import EnqueueRequest, QueueItem


def _sent(route) -> object:
    return json.loads(route.calls.last.request.content)


# ---- enqueue (Decision 11) -------------------------------------------------------------


def _queue(respx_mock, answer=None):
    return respx_mock.post(f"{API}/queue/").respond(200, json=answer or load("q4.json"))


async def test_enqueue_body_model_target(respx_mock):
    route = _queue(respx_mock)
    item = await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, None))
    assert route.call_count == 1
    assert route.calls.last.request.url.path == "/api/v1/queue/"
    assert _sent(route) == {
        "library_file_id": 5,
        "plate_id": 1,
        "target_model": "A1",
        "manual_start": False,
    }
    assert isinstance(item, QueueItem)
    assert item.status == "pending"
    assert item.id == load("q4.json")["id"]


async def test_enqueue_body_matches_request_fixture(respx_mock):
    route = _queue(respx_mock)
    await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, None))
    assert _sent(route) == load("q4.request.json")


async def test_enqueue_body_printer_target_and_top(respx_mock):
    route = _queue(respx_mock)
    await client().enqueue(EnqueueRequest(5, 1, None, 2, True, None))
    assert _sent(route) == {
        "library_file_id": 5,
        "plate_id": 1,
        "printer_id": 2,
        "manual_start": False,
        "insert_at_top": True,
    }


async def test_enqueue_insert_position(respx_mock):
    route = _queue(respx_mock)
    await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, 3))
    assert _sent(route) == {
        "library_file_id": 5,
        "plate_id": 1,
        "target_model": "A1",
        "manual_start": False,
        "insert_position": 3,
    }


@pytest.mark.parametrize(
    "req",
    [
        EnqueueRequest(5, 1, "A1", 2, False, None),  # both targets
        EnqueueRequest(5, 1, None, None, False, None),  # no target
        EnqueueRequest(5, 1, "", None, False, None),  # an empty model is no target
        EnqueueRequest(5, 1, "A1", None, False, 0),  # positions start at 1
        EnqueueRequest(5, 1, "A1", None, False, -1),
    ],
    ids=["both-targets", "no-target", "empty-model", "position-0", "position-negative"],
)
async def test_enqueue_invalid_request_sends_nothing(respx_mock, req):
    route = _queue(respx_mock)
    with pytest.raises(ValueError):
        await client().enqueue(req)
    assert route.call_count == 0


async def test_enqueue_timeout_is_unavailable_and_sent_once(respx_mock):
    """Review Focus 1: the outcome is unknown, and the client never sends it again."""
    route = respx_mock.post(f"{API}/queue/").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(BambuddyUnavailable):
        await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, None))
    assert route.call_count == 1


async def test_enqueue_server_error_is_unavailable_and_sent_once(respx_mock):
    route = respx_mock.post(f"{API}/queue/").respond(502)
    with pytest.raises(BambuddyUnavailable):
        await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, None))
    assert route.call_count == 1


async def test_enqueue_rejected(respx_mock):
    respx_mock.post(f"{API}/queue/").respond(400, json={"detail": "No active printers for model: A1"})
    with pytest.raises(BambuddyRejected) as info:
        await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, None))
    assert info.value.detail == "No active printers for model: A1"
    assert info.value.status == 400


async def test_enqueue_answer_shows_review_wait(respx_mock):
    _queue(respx_mock, dict(load("q4.json"), manual_start=True))
    item = await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, None))
    assert item.manual_start is True


async def test_enqueue_half_answer_unexpected(respx_mock):
    _queue(respx_mock, {"id": 31})
    with pytest.raises(BambuddyUnexpected):
        await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, None))


# ---- single entries, no batch (Spec 7.6 since v0.3.5) ----------------------------------


@pytest.mark.parametrize(
    "req",
    [
        EnqueueRequest(5, 1, "A1", None, False, None),
        EnqueueRequest(5, 2, None, 2, True, None),
        EnqueueRequest(5, 1, "A1", None, False, 3),
    ],
    ids=["model", "printer-top", "position"],
)
async def test_enqueue_body_has_no_batch_and_no_quantity(respx_mock, req):
    """Without `batch_id` and `quantity` Bambuddy makes exactly one entry outside any batch."""
    route = _queue(respx_mock)
    await client().enqueue(req)
    body = _sent(route)
    assert "batch_id" not in body and "quantity" not in body


async def test_enqueue_request_has_no_batch_field():
    assert "batch_id" not in {f.name for f in dataclasses.fields(EnqueueRequest)}


async def test_no_batch_route_is_called_by_enqueue(respx_mock):
    """A hand-over's only write is `POST /queue/`; `/queue/batches` is never asked."""
    batches = respx_mock.route(url__regex=r".*/queue/batches.*").respond(200, json={})
    route = _queue(respx_mock)
    await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, None))
    assert route.call_count == 1
    assert batches.call_count == 0


def test_batch_api_is_gone():
    for name in ("ensure_batch", "find_items_by_batch"):
        assert not hasattr(HttpBambuddyClient, name), name
        assert not hasattr(BambuddyClient, name), name
    assert not hasattr(client_module, "BatchRefLost")
    for name in ("EXTERNAL_SOURCE", "parse_batch_id", "parse_batch_hits"):
        assert not hasattr(parse, name), name
