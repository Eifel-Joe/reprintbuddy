"""Deleting an own waiting entry from Bambuddy's queue after a cancel (Spec 7.2a, 0.4.1 p382).

Bambuddy (`backend/app/api/routes/print_queue.py:2697-2748`, `delete_queue_item`): 200 with
`{"deleted": true}`, or `{"deleted": false}` when it keeps the entry as cancelled (#2960); 404 for an
unknown id; 400 "Cannot delete item that is currently printing"; 403 without the right.
"""

import httpx
import pytest
from fixtures import API, KEY, client

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable, BambuddyUnexpected


def _route(respx_mock, status=200, json=None):
    return respx_mock.delete(f"{API}/queue/7").respond(status, json=json)


async def test_deleted(respx_mock):
    route = _route(respx_mock, json={"message": "Queue item deleted", "deleted": True})
    assert await client().delete_queue_item(7) == "deleted"
    request = route.calls.last.request
    assert (request.method, request.url.path, request.url.query) == ("DELETE", "/api/v1/queue/7", b"")
    assert request.headers["X-API-Key"] == KEY
    assert request.content == b""


async def test_kept_as_cancelled(respx_mock):
    _route(respx_mock, json={"message": "Item cancelled rather than deleted", "deleted": False})
    assert await client().delete_queue_item(7) == "cancelled"


async def test_unknown_entry_is_gone(respx_mock):
    _route(respx_mock, 404, json={"detail": "Queue item not found"})
    assert await client().delete_queue_item(7) == "gone"


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (400, "Cannot delete item that is currently printing"),
        (403, "API key owner does not have 'queue:delete_own' permission"),
        (403, "You can only delete your own queue items"),
    ],
)
async def test_refusals(respx_mock, status, detail):
    _route(respx_mock, status, json={"detail": detail})
    with pytest.raises(BambuddyRejected) as info:
        await client().delete_queue_item(7)
    assert (info.value.status, info.value.detail) == (status, detail)


async def test_server_error_and_timeout_are_unavailable(respx_mock):
    _route(respx_mock, 500)
    with pytest.raises(BambuddyUnavailable):
        await client().delete_queue_item(7)
    respx_mock.delete(f"{API}/queue/8").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(BambuddyUnavailable):
        await client().delete_queue_item(8)


@pytest.mark.parametrize("body", [{"message": "ok"}, {"deleted": "yes"}, [1]])
async def test_an_answer_without_the_flag_is_unexpected(respx_mock, body):
    _route(respx_mock, json=body)
    with pytest.raises(BambuddyUnexpected):
        await client().delete_queue_item(7)
