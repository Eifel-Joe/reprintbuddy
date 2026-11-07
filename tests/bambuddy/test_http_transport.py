"""The HTTP transport of `HttpBambuddyClient` (B2 Task 1, Decisions 2-4, Review Focus 3-5)."""

import json
import logging

import httpx
import pytest
from fixtures import API, KEY, URL, client, load

from reprintbuddy.bambuddy.client import (
    BambuddyRejected,
    BambuddyUnavailable,
    BambuddyUnexpected,
    BatchRefLost,
    HttpBambuddyClient,
)
from reprintbuddy.bambuddy.connection import TIMEOUT_SECONDS


def q4_with_id(item_id: int) -> dict:
    return {**load("q4.json"), "id": item_id}


# ---- error classes (Decision 2) ---------------------------------------------------


def test_error_class_tree():
    assert issubclass(BambuddyUnexpected, BambuddyUnavailable)
    assert issubclass(BatchRefLost, BambuddyRejected)
    assert not issubclass(BambuddyRejected, BambuddyUnavailable)


def test_rejected_keeps_the_old_call_form():
    """`BambuddyRejected("...")` from B3 and B4 still works."""
    exc = BambuddyRejected("unknown file 3")
    assert (exc.detail, exc.status) == ("unknown file 3", None)
    assert str(exc) == "unknown file 3"


def test_rejected_with_status():
    exc = BambuddyRejected("Authentication required", status=401)
    assert (exc.detail, exc.status) == ("Authentication required", 401)
    assert str(exc) == "401: Authentication required"


# ---- reading one entry ------------------------------------------------------------


async def test_get_queue_item(respx_mock):
    route = respx_mock.get(f"{API}/queue/7").respond(200, json=q4_with_id(7))
    item = await client().get_queue_item(7)
    assert item.id == 7
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers.get_list("X-API-Key") == [KEY]
    assert KEY not in str(request.url)
    assert "authorization" not in request.headers


async def test_get_queue_item_404_is_none(respx_mock):
    respx_mock.get(f"{API}/queue/7").respond(404, json=load("queue_404.json"))
    assert await client().get_queue_item(7) is None


# ---- status codes and network errors ---------------------------------------------


@pytest.mark.parametrize(
    "answer, expected, status, detail",
    [
        ({"status_code": 401, "json": load("auth_401.json")}, BambuddyRejected, 401, "Authentication required"),
        ({"status_code": 403, "json": {"detail": "Insufficient permissions"}}, BambuddyRejected, 403, "Insufficient permissions"),
        ({"status_code": 500, "text": "boom"}, BambuddyUnavailable, None, None),
        ({"status_code": 502, "text": "bad gateway"}, BambuddyUnavailable, None, None),
        ({"side_effect": httpx.ConnectError("refused")}, BambuddyUnavailable, None, None),
        ({"side_effect": httpx.ReadTimeout("slow")}, BambuddyUnavailable, None, None),
        ({"status_code": 200, "html": "<html>router login</html>"}, BambuddyUnexpected, None, None),
    ],
    ids=["401", "403", "500", "502", "connect-error", "read-timeout", "html"],
)
async def test_status_mapping(respx_mock, answer, expected, status, detail):
    route = respx_mock.get(f"{API}/queue/7")
    if "side_effect" in answer:
        route.mock(side_effect=answer["side_effect"])
    else:
        route.respond(**answer)
    with pytest.raises(expected) as info:
        await client().get_queue_item(7)
    assert type(info.value) is expected
    if expected is BambuddyRejected:
        assert (info.value.status, info.value.detail) == (status, detail)


async def test_server_error_is_not_unexpected(respx_mock):
    """5xx means: outcome unknown, not a wrong answer."""
    respx_mock.get(f"{API}/queue/7").respond(503, text="down")
    with pytest.raises(BambuddyUnavailable) as info:
        await client().get_queue_item(7)
    assert not isinstance(info.value, BambuddyUnexpected)


async def test_detail_object_is_text(respx_mock):
    detail = {"code": "x", "message": "y"}
    respx_mock.get(f"{API}/queue/7").respond(400, json={"detail": detail})
    with pytest.raises(BambuddyRejected) as info:
        await client().get_queue_item(7)
    assert info.value.status == 400
    assert info.value.detail == json.dumps(detail)


@pytest.mark.parametrize(
    "answer",
    [{"text": "not json"}, {"json": {"message": "no detail"}}, {"json": ["a", "b"]}],
    ids=["text", "no-detail", "list"],
)
async def test_detail_missing_is_empty(respx_mock, answer):
    respx_mock.get(f"{API}/queue/7").respond(409, **answer)
    with pytest.raises(BambuddyRejected) as info:
        await client().get_queue_item(7)
    assert (info.value.status, info.value.detail) == (409, "")


async def test_404_without_none_is_rejected(respx_mock):
    respx_mock.get(f"{API}/library/files/5/plates").respond(404, json=load("file_404.json"))
    with pytest.raises(BambuddyRejected) as info:
        await client()._get("/library/files/5/plates")
    assert (info.value.status, info.value.detail) == (404, "File not found")


async def test_redirect_not_followed(respx_mock):
    """Review Focus 4: the key never goes anywhere else."""
    respx_mock.get(f"{API}/queue/7").respond(307, headers={"Location": "http://elsewhere.test/"})
    elsewhere = respx_mock.get("http://elsewhere.test/").respond(200, json=q4_with_id(7))
    with pytest.raises(BambuddyUnexpected):
        await client().get_queue_item(7)
    assert elsewhere.call_count == 0


async def test_redirect_not_followed_even_if_the_factory_says_so(respx_mock):
    respx_mock.get(f"{API}/queue/7").respond(307, headers={"Location": "http://elsewhere.test/"})
    elsewhere = respx_mock.get("http://elsewhere.test/").respond(200, json=q4_with_id(7))
    following = client(lambda: httpx.AsyncClient(follow_redirects=True))
    with pytest.raises(BambuddyUnexpected):
        await following.get_queue_item(7)
    assert elsewhere.call_count == 0


async def test_trailing_slash_in_base_url(respx_mock):
    route = respx_mock.get(f"{URL}/api/v1/queue/7").respond(200, json=q4_with_id(7))
    bambuddy = HttpBambuddyClient("http://bambuddy.test:8000/", KEY, lambda: httpx.AsyncClient())
    assert (await bambuddy.get_queue_item(7)).id == 7
    assert str(route.calls.last.request.url) == "http://bambuddy.test:8000/api/v1/queue/7"


async def test_timeout_is_ten_seconds(respx_mock):
    route = respx_mock.get(f"{API}/queue/7").respond(200, json=q4_with_id(7))
    await client().get_queue_item(7)
    assert TIMEOUT_SECONDS == 10.0
    assert route.calls.last.request.extensions["timeout"] == httpx.Timeout(10.0).as_dict()


async def test_invalid_url_is_unavailable():
    bambuddy = HttpBambuddyClient("http://", KEY, lambda: httpx.AsyncClient())
    with pytest.raises(BambuddyUnavailable) as info:
        await bambuddy.get_queue_item(7)
    assert not isinstance(info.value, BambuddyUnexpected)


@pytest.mark.parametrize(
    "answer",
    [
        {"status_code": 401, "json": load("auth_401.json")},
        {"status_code": 500, "text": "boom"},
        {"side_effect": httpx.ConnectError("refused")},
    ],
    ids=["401", "500", "network"],
)
async def test_key_never_leaks(respx_mock, caplog, answer):
    """Review Focus 5: not in the message, not in repr, not in the log."""
    caplog.set_level(logging.DEBUG)
    route = respx_mock.get(f"{API}/queue/7")
    if "side_effect" in answer:
        route.mock(side_effect=answer["side_effect"])
    else:
        route.respond(**answer)
    bambuddy = client()
    with pytest.raises((BambuddyRejected, BambuddyUnavailable)) as info:
        await bambuddy.get_queue_item(7)
    assert KEY not in str(info.value)
    assert KEY not in repr(info.value)
    assert KEY not in repr(bambuddy)
    assert KEY not in caplog.text


def test_repr_names_only_the_address():
    assert repr(client()) == f"HttpBambuddyClient({URL})"


# ---- the other two helpers ----------------------------------------------------------


async def test_get_passes_params(respx_mock):
    route = respx_mock.get(f"{API}/queue/", params={"status": "pending,printing"}).respond(
        200, json=load("queue_open.json")
    )
    body = await client()._get("/queue/", {"status": "pending,printing"})
    assert body == load("queue_open.json")
    assert route.call_count == 1


async def test_get_json_null_is_unexpected(respx_mock):
    """`None` is reserved for "gone" (404); a 200 with `null` is not an answer of the source."""
    respx_mock.get(f"{API}/queue/7").respond(200, json=None)
    with pytest.raises(BambuddyUnexpected):
        await client().get_queue_item(7)


async def test_post_sends_json_with_key(respx_mock):
    route = respx_mock.post(f"{API}/queue/batches").respond(200, json=load("batch1.json"))
    body = load("batch1.request.json")
    answer = await client()._post("/queue/batches", body)
    assert answer == load("batch1.json")
    request = route.calls.last.request
    assert json.loads(request.content) == body
    assert request.headers.get_list("X-API-Key") == [KEY]
    assert request.headers["content-type"] == "application/json"
    assert request.extensions["timeout"] == httpx.Timeout(10.0).as_dict()


async def test_post_conflict_is_rejected(respx_mock):
    respx_mock.post(f"{API}/queue/batches").respond(409, json=load("batch1_again.json"))
    with pytest.raises(BambuddyRejected) as info:
        await client()._post("/queue/batches", load("batch1.request.json"))
    assert info.value.status == 409
    assert info.value.detail == load("batch1_again.json")["detail"]


async def test_post_timeout_is_unavailable_and_sent_once(respx_mock):
    route = respx_mock.post(f"{API}/queue/").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(BambuddyUnavailable):
        await client()._post("/queue/", load("q4.request.json"))
    assert route.call_count == 1


async def test_post_without_json_is_unexpected(respx_mock):
    respx_mock.post(f"{API}/queue/").respond(200, text="ok")
    with pytest.raises(BambuddyUnexpected):
        await client()._post("/queue/", load("q4.request.json"))


async def test_get_bytes(respx_mock):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    route = respx_mock.get(f"{API}/library/files/5/plate-thumbnail/1").respond(
        200, content=png, headers={"Content-Type": "image/png"}
    )
    content_type, content = await client()._get_bytes("/library/files/5/plate-thumbnail/1")
    assert (content_type, content) == ("image/png", png)
    assert route.calls.last.request.headers.get_list("X-API-Key") == [KEY]


async def test_get_bytes_404_is_rejected(respx_mock):
    respx_mock.get(f"{API}/library/files/5/plate-thumbnail/99").respond(404, json=load("plate_404.json"))
    with pytest.raises(BambuddyRejected) as info:
        await client()._get_bytes("/library/files/5/plate-thumbnail/99")
    assert (info.value.status, info.value.detail) == (404, "Thumbnail for plate 99 not found")


async def test_one_http_client_per_call(respx_mock):
    """Decision 4: each method opens its own client through the factory."""
    respx_mock.get(f"{API}/queue/7").respond(200, json=q4_with_id(7))
    opened = []

    def factory():
        opened.append(1)
        return httpx.AsyncClient()

    bambuddy = client(factory)
    await bambuddy.get_queue_item(7)
    await bambuddy.get_queue_item(7)
    assert len(opened) == 2
