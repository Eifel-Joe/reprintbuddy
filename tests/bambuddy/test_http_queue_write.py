"""Creating batches and queue entries in Bambuddy (B2 Task 6, Decisions 10 and 11)."""

import json

import httpx
import pytest
from fixtures import API, client, load

from reprintbuddy.bambuddy.client import (
    BambuddyRejected,
    BambuddyUnavailable,
    BambuddyUnexpected,
    BatchRefLost,
)
from reprintbuddy.bambuddy import parse
from reprintbuddy.bambuddy.types import EnqueueRequest, QueueItem

REF = "00000000-0000-4000-8000-000000000001"  # the reference of batch1*.json
OTHER_REF = "00000000-0000-4000-8000-000000000002"


def _sent(route) -> object:
    return json.loads(route.calls.last.request.content)


def _params(route) -> list[tuple[str, str]]:
    return list(route.calls.last.request.url.params.multi_items())


def _search(respx_mock, answer):
    return respx_mock.get(f"{API}/queue/batches").respond(200, json=answer)


# ---- ensure_batch (Decision 10) --------------------------------------------------------


async def test_ensure_batch_creates(respx_mock):
    route = respx_mock.post(f"{API}/queue/batches").respond(200, json=load("batch1.json"))
    batch_id = await client().ensure_batch("Reprint 1", REF, 5)
    assert batch_id == load("batch1.json")["id"]
    assert route.call_count == 1
    assert route.calls.last.request.url.path == "/api/v1/queue/batches"
    assert _sent(route) == {
        "name": "Reprint 1",
        "library_file_id": 5,
        "external_source": "reprintbuddy",
        "external_ref": REF,
    }


async def test_batch_body_matches_request_fixture(respx_mock):
    route = respx_mock.post(f"{API}/queue/batches").respond(200, json=load("batch1.json"))
    await client().ensure_batch("Template A", REF, 5)
    assert _sent(route) == load("batch1.request.json")


async def test_batch_conflict_finds_existing(respx_mock):
    post = respx_mock.post(f"{API}/queue/batches").respond(409, json=load("batch1_again.json"))
    search = _search(respx_mock, load("batch1_find.json"))
    batch_id = await client().ensure_batch("Reprint 1", REF, 5)
    assert batch_id == load("batch1_find.json")[0]["id"]
    assert post.call_count == 1  # no second create
    assert search.call_count == 1
    assert search.calls.last.request.url.path == "/api/v1/queue/batches"
    assert _params(search) == [("external_source", "reprintbuddy"), ("external_ref", REF)]


async def test_batch_conflict_without_hit_is_lost(respx_mock):
    """Review Focus 2: an empty batch from a lost answer is never reused, and nothing crashes."""
    post = respx_mock.post(f"{API}/queue/batches").respond(409, json=load("batch1_again.json"))
    _search(respx_mock, load("batch1_find_empty.json"))
    with pytest.raises(BatchRefLost) as info:
        await client().ensure_batch("Reprint 1", REF, 5)
    exc = info.value
    assert isinstance(exc, BambuddyRejected)
    assert exc.status == 409
    assert exc.detail == load("batch1_again.json")["detail"]
    assert post.call_count == 1


async def test_batch_conflict_two_hits_unexpected(respx_mock):
    respx_mock.post(f"{API}/queue/batches").respond(409, json=load("batch1_again.json"))
    first = load("batch1_find.json")[0]
    _search(respx_mock, [first, dict(first, id=first["id"] + 1)])
    with pytest.raises(BambuddyUnexpected):
        await client().ensure_batch("Reprint 1", REF, 5)


async def test_batch_conflict_hit_for_other_ref_unexpected(respx_mock):
    respx_mock.post(f"{API}/queue/batches").respond(409, json=load("batch1_again.json"))
    _search(respx_mock, [dict(load("batch1_find.json")[0], external_ref=OTHER_REF)])
    with pytest.raises(BambuddyUnexpected):
        await client().ensure_batch("Reprint 1", REF, 5)


async def test_batch_search_not_a_list_unexpected(respx_mock):
    respx_mock.post(f"{API}/queue/batches").respond(409, json=load("batch1_again.json"))
    _search(respx_mock, load("batch1.json"))
    with pytest.raises(BambuddyUnexpected):
        await client().ensure_batch("Reprint 1", REF, 5)


async def test_batch_answer_for_other_ref_unexpected(respx_mock):
    respx_mock.post(f"{API}/queue/batches").respond(
        200, json=dict(load("batch1.json"), external_ref=OTHER_REF)
    )
    with pytest.raises(BambuddyUnexpected):
        await client().ensure_batch("Reprint 1", REF, 5)


@pytest.mark.parametrize("status", [400, 403, 404, 422])
async def test_batch_other_rejection_passes_through(respx_mock, status):
    """Only a 409 means "exists already"; every other 4xx stays a plain rejection."""
    route = respx_mock.post(f"{API}/queue/batches").respond(status, json={"detail": "no"})
    with pytest.raises(BambuddyRejected) as info:
        await client().ensure_batch("Reprint 1", REF, 5)
    assert not isinstance(info.value, BatchRefLost)
    assert info.value.status == status
    assert route.call_count == 1


async def test_batch_timeout_is_unavailable_and_sent_once(respx_mock):
    route = respx_mock.post(f"{API}/queue/batches").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(BambuddyUnavailable):
        await client().ensure_batch("Reprint 1", REF, 5)
    assert route.call_count == 1


def test_parse_batch_id():
    assert parse.EXTERNAL_SOURCE == "reprintbuddy"
    assert parse.parse_batch_id(load("batch1.json"), REF) == 12


@pytest.mark.parametrize(
    "change",
    [
        {"external_source": "other"},
        {"external_source": None},
        {"external_ref": OTHER_REF},
        {"external_ref": None},
        {"id": "12"},
        {"id": None},
        {"id": True},
    ],
)
def test_parse_batch_id_rejects(change):
    with pytest.raises(BambuddyUnexpected):
        parse.parse_batch_id(dict(load("batch1.json"), **change), REF)


def test_parse_batch_id_rejects_missing_id_and_non_object():
    data = load("batch1.json")
    del data["id"]
    with pytest.raises(BambuddyUnexpected):
        parse.parse_batch_id(data, REF)
    with pytest.raises(BambuddyUnexpected):
        parse.parse_batch_id([load("batch1.json")], REF)


# ---- enqueue (Decision 11) -------------------------------------------------------------


def _queue(respx_mock, answer=None):
    return respx_mock.post(f"{API}/queue/").respond(200, json=answer or load("q4.json"))


async def test_enqueue_body_model_target(respx_mock):
    route = _queue(respx_mock)
    item = await client().enqueue(EnqueueRequest(5, 1, "A1", None, 9, False, None))
    assert route.call_count == 1
    assert route.calls.last.request.url.path == "/api/v1/queue/"
    assert _sent(route) == {
        "library_file_id": 5,
        "plate_id": 1,
        "target_model": "A1",
        "batch_id": 9,
        "manual_start": False,
    }
    assert isinstance(item, QueueItem)
    assert item.status == "pending"
    assert item.id == load("q4.json")["id"]


async def test_enqueue_body_matches_request_fixture(respx_mock):
    route = _queue(respx_mock)
    await client().enqueue(EnqueueRequest(5, 1, "A1", None, 12, False, None))
    assert _sent(route) == load("q4.request.json")


async def test_enqueue_body_printer_target_and_top(respx_mock):
    route = _queue(respx_mock)
    await client().enqueue(EnqueueRequest(5, 1, None, 2, 9, True, None))
    assert _sent(route) == {
        "library_file_id": 5,
        "plate_id": 1,
        "printer_id": 2,
        "batch_id": 9,
        "manual_start": False,
        "insert_at_top": True,
    }


async def test_enqueue_insert_position(respx_mock):
    route = _queue(respx_mock)
    await client().enqueue(EnqueueRequest(5, 1, "A1", None, 9, False, 3))
    assert _sent(route) == {
        "library_file_id": 5,
        "plate_id": 1,
        "target_model": "A1",
        "batch_id": 9,
        "manual_start": False,
        "insert_position": 3,
    }


@pytest.mark.parametrize(
    "req",
    [
        EnqueueRequest(5, 1, "A1", 2, 9, False, None),  # both targets
        EnqueueRequest(5, 1, None, None, 9, False, None),  # no target
        EnqueueRequest(5, 1, "", None, 9, False, None),  # an empty model is no target
        EnqueueRequest(5, 1, "A1", None, 9, False, 0),  # positions start at 1
        EnqueueRequest(5, 1, "A1", None, 9, False, -1),
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
        await client().enqueue(EnqueueRequest(5, 1, "A1", None, 9, False, None))
    assert route.call_count == 1


async def test_enqueue_server_error_is_unavailable_and_sent_once(respx_mock):
    route = respx_mock.post(f"{API}/queue/").respond(502)
    with pytest.raises(BambuddyUnavailable):
        await client().enqueue(EnqueueRequest(5, 1, "A1", None, 9, False, None))
    assert route.call_count == 1


async def test_enqueue_rejected(respx_mock):
    respx_mock.post(f"{API}/queue/").respond(400, json={"detail": "No active printers for model: A1"})
    with pytest.raises(BambuddyRejected) as info:
        await client().enqueue(EnqueueRequest(5, 1, "A1", None, 9, False, None))
    assert info.value.detail == "No active printers for model: A1"
    assert info.value.status == 400


async def test_enqueue_answer_shows_review_wait(respx_mock):
    _queue(respx_mock, dict(load("q4.json"), manual_start=True))
    item = await client().enqueue(EnqueueRequest(5, 1, "A1", None, 9, False, None))
    assert item.manual_start is True


async def test_enqueue_half_answer_unexpected(respx_mock):
    _queue(respx_mock, {"id": 31})
    with pytest.raises(BambuddyUnexpected):
        await client().enqueue(EnqueueRequest(5, 1, "A1", None, 9, False, None))
